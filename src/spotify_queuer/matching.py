"""Shared fuzzy matcher for Spotify tracks <-> local library files.

Per Build Spec §9: normalized artist/title comparison, rapidfuzz title
similarity (85% weight) + artist-overlap bonus (15% weight), acceptance
threshold configurable (default 87/100).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|/|;|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\band\b)\s*", re.IGNORECASE)
_NORMALIZE_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

DEFAULT_THRESHOLD = 87
TITLE_WEIGHT = 0.85
ARTIST_WEIGHT = 0.15


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.casefold()
    text = _NORMALIZE_STRIP_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def split_artists(artists: str | list[str]) -> list[str]:
    """Split a raw artist string (or list of raw artist strings) into normalized names."""
    if isinstance(artists, str):
        raw_parts = _ARTIST_SPLIT_RE.split(artists)
    else:
        raw_parts = []
        for a in artists:
            raw_parts.extend(_ARTIST_SPLIT_RE.split(a))
    return [normalize_text(p) for p in raw_parts if normalize_text(p)]


def title_similarity(title_a: str, title_b: str) -> float:
    """0-100 similarity score between two track titles."""
    return fuzz.token_sort_ratio(normalize_text(title_a), normalize_text(title_b))


def artist_overlap_score(artists_a: list[str], artists_b: list[str]) -> float:
    """0-100 overlap score between two sets of normalized artist names."""
    set_a, set_b = set(artists_a), set(artists_b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    if intersection:
        return 100.0 * len(intersection) / max(len(set_a), len(set_b))
    # No exact-name overlap: fall back to best fuzzy pairing so minor
    # spelling/formatting differences ("Disclosure" vs "disclosure uk") still score.
    best_pairs = 0.0
    for a in set_a:
        for b in set_b:
            best_pairs = max(best_pairs, fuzz.ratio(a, b))
    return best_pairs * 0.5  # discount fuzzy-only artist matches vs exact ones


@dataclass(frozen=True)
class MatchResult:
    score: float
    title_score: float
    artist_score: float
    is_match: bool


def score_match(
    title_a: str,
    artists_a: str | list[str],
    title_b: str,
    artists_b: str | list[str],
    threshold: int = DEFAULT_THRESHOLD,
) -> MatchResult:
    """Score a candidate pairing; combined = 85% title + 15% artist overlap."""
    t_score = title_similarity(title_a, title_b)
    a_score = artist_overlap_score(split_artists(artists_a), split_artists(artists_b))
    combined = TITLE_WEIGHT * t_score + ARTIST_WEIGHT * a_score
    return MatchResult(score=combined, title_score=t_score, artist_score=a_score, is_match=combined >= threshold)


def _min_title_score(threshold: float) -> float:
    """Lowest title score that could still reach `threshold` given a perfect
    artist score -- anything below it can be discarded without scoring artists."""
    # Small epsilon so float rounding never drops a pairing that would land exactly on the threshold.
    return max(0.0, (threshold - ARTIST_WEIGHT * 100.0) / TITLE_WEIGHT - 1e-6)


def _sort_tokens(text: str) -> str:
    return " ".join(sorted(text.split()))


class MatchIndex:
    """Candidates normalized once up front, so matching many queries against
    the same (large) local library doesn't redo the regex normalization for
    every pairing. Titles are compared in one batched rapidfuzz call per query
    (C++, with a cutoff that discards hopeless candidates before the Python-
    level artist scoring runs), giving the same results as scoring every
    pairing with score_match().

    `keys`, if given, maps a candidate to several (title, artists) pairs
    (e.g. one from the filename and one from its tags); a candidate's score
    is its best-scoring key. Otherwise each candidate has the single key
    (title_attr, artists_attr)."""

    def __init__(self, candidates, title_attr: str = "title", artists_attr: str = "artists", keys=None):
        self._candidates = list(candidates)
        if keys is None:
            keys = lambda c: [(getattr(c, title_attr), getattr(c, artists_attr))]  # noqa: E731
        # Words pre-sorted so the per-query comparison can use plain fuzz.ratio:
        # ratio(sorted(a), sorted(b)) is exactly token_sort_ratio(a, b), minus
        # re-sorting every candidate's words on every query.
        self._sorted_titles: list[str] = []
        self._artist_lists: list[list[str]] = []
        self._owners: list[int] = []  # key position -> candidate position
        for owner, candidate in enumerate(self._candidates):
            for title, artists in keys(candidate):
                self._sorted_titles.append(_sort_tokens(normalize_text(title)))
                self._artist_lists.append(split_artists(artists))
                self._owners.append(owner)

    def __len__(self) -> int:
        return len(self._candidates)

    def best(self, title: str, artists: str | list[str], threshold: int = DEFAULT_THRESHOLD):
        """Return (candidate, MatchResult) for the best candidate scoring >= threshold, or (None, None).
        Ties go to the earliest candidate, as with best_match()."""
        if not self._candidates:
            return None, None
        hits = process.extract(
            _sort_tokens(normalize_text(title)),
            self._sorted_titles,
            scorer=fuzz.ratio,
            processor=None,
            score_cutoff=_min_title_score(threshold),
            limit=None,
        )
        if not hits:
            return None, None
        query_artists = split_artists(artists)
        best_owner = -1
        best_result: MatchResult | None = None
        for _, t_score, index in sorted(hits, key=lambda hit: hit[2]):
            a_score = artist_overlap_score(query_artists, self._artist_lists[index])
            combined = TITLE_WEIGHT * t_score + ARTIST_WEIGHT * a_score
            if combined >= threshold and (best_result is None or combined > best_result.score):
                best_owner = self._owners[index]
                best_result = MatchResult(score=combined, title_score=t_score, artist_score=a_score, is_match=True)
        if best_result is None:
            return None, None
        return self._candidates[best_owner], best_result


def best_match(
    title: str,
    artists: str | list[str],
    candidates,
    threshold: int = DEFAULT_THRESHOLD,
    candidate_title_attr: str = "title",
    candidate_artists_attr: str = "artists",
):
    """Return (candidate, MatchResult) for the best candidate scoring >= threshold, or (None, None).

    `candidates` is any iterable of objects exposing `candidate_title_attr` and
    `candidate_artists_attr`. When matching many queries against the same
    candidates, build one MatchIndex and reuse it instead.
    """
    return MatchIndex(candidates, candidate_title_attr, candidate_artists_attr).best(title, artists, threshold)
