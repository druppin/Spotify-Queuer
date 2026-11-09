from spotify_queuer import matching


def test_normalize_text_strips_punctuation_and_case():
    assert matching.normalize_text("Don't Stop (Radio Edit)!") == "don t stop radio edit"


def test_title_similarity_identical():
    assert matching.title_similarity("Latch", "Latch") == 100


def test_split_artists_handles_common_separators():
    assert matching.split_artists("Disclosure feat. Sam Smith") == ["disclosure", "sam smith"]
    assert matching.split_artists("A & B, C") == ["a", "b", "c"]


def test_artist_overlap_exact_match():
    assert matching.artist_overlap_score(["disclosure"], ["disclosure"]) == 100.0


def test_artist_overlap_no_data_is_zero():
    assert matching.artist_overlap_score([], ["disclosure"]) == 0.0


def test_score_match_accepts_close_match_above_threshold():
    result = matching.score_match(
        "Latch", "Disclosure feat. Sam Smith", "Latch", "Disclosure, Sam Smith", threshold=87
    )
    assert result.is_match
    assert result.score >= 87


def test_score_match_rejects_different_song():
    result = matching.score_match("Latch", "Disclosure", "White Noise", "Disclosure", threshold=87)
    assert not result.is_match


def test_best_match_picks_highest_scoring_candidate():
    class Candidate:
        def __init__(self, title, artists, path):
            self.title = title
            self.artists = artists
            self.path = path

    candidates = [
        Candidate("Latch (Acoustic)", "Disclosure", "a"),
        Candidate("Latch", "Disclosure, Sam Smith", "b"),
    ]
    best, result = matching.best_match("Latch", "Disclosure, Sam Smith", candidates, threshold=87)
    assert best.path == "b"


def test_best_match_returns_none_when_no_candidate_clears_threshold():
    class Candidate:
        def __init__(self, title, artists, path):
            self.title = title
            self.artists = artists
            self.path = path

    candidates = [Candidate("Completely Different Song", "Someone Else", "a")]
    best, result = matching.best_match("Latch", "Disclosure, Sam Smith", candidates, threshold=87)
    assert best is None
    assert result is None


def test_match_index_agrees_with_brute_force_scoring():
    import random

    class Candidate:
        def __init__(self, title, artists):
            self.title = title
            self.artists = artists

    words = ["latch", "white", "noise", "omen", "you", "me", "help", "remix", "edit", "radio", "the", "love"]
    artists = ["Disclosure", "Sam Smith", "AlunaGeorge", "Eliza Doolittle", "Flume", "Kai"]
    rng = random.Random(1)

    def random_track():
        title = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
        return title, ", ".join(rng.sample(artists, rng.randint(1, 2)))

    candidates = [Candidate(*random_track()) for _ in range(300)]
    index = matching.MatchIndex(candidates)
    for _ in range(100):
        title, artist_str = random_track()
        for threshold in (60, 87, 95):
            expected_best, expected_score = None, None
            for c in candidates:
                r = matching.score_match(title, artist_str, c.title, c.artists, threshold=threshold)
                if r.is_match and (expected_score is None or r.score > expected_score):
                    expected_best, expected_score = c, r.score
            best, result = index.best(title, artist_str, threshold=threshold)
            assert best is expected_best
            if expected_best is not None:
                assert abs(result.score - expected_score) < 1e-9


def test_exact_title_with_strangers_artist_is_not_a_match():
    # 85 (title) + 2.5 (weak fuzzy artist) = 87.5 used to pass the 87 threshold.
    result = matching.score_match("Latch", "Disclosure", "Latch", "Someone Else")
    assert result.score >= 87 and not result.is_match  # would have passed before
    assert matching.best_match("Latch", "Someone Else", [type("T", (), {"title": "Latch", "artists": "Disclosure"})()]) == (None, None)
    assert matching.score_match("Latch", "Disclosure feat. Sam Smith", "Latch", "Disclosure").is_match
