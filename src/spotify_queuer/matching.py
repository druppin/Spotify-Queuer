"""Shared fuzzy matcher for Spotify tracks <-> local library files.

Per Build Spec §9: normalized artist/title comparison, rapidfuzz title
similarity (85% weight) + artist-overlap bonus (15% weight), acceptance
threshold configurable (default 87/100).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

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
    `candidate_artists_attr`.
    """
    best_candidate = None
    best_result: MatchResult | None = None
    for candidate in candidates:
        cand_title = getattr(candidate, candidate_title_attr)
        cand_artists = getattr(candidate, candidate_artists_attr)
        result = score_match(title, artists, cand_title, cand_artists, threshold=threshold)
        if result.score >= threshold and (best_result is None or result.score > best_result.score):
            best_candidate, best_result = candidate, result
    return best_candidate, best_result
