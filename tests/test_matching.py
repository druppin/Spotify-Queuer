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
