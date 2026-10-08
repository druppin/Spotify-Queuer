from spotify_queuer.rate_limiter import RateLimiter
from spotify_queuer.spotify_client import SpotifyClient


class FakeRawSpotify:
    """Stands in for spotipy.Spotify's underlying HTTP calls."""

    def __init__(self, pages):
        self._pages = pages

    def playlist_items(self, playlist_id, limit=100, additional_types=("track",)):
        return self._pages[0]

    def next(self, results):
        idx = self._pages.index(results) + 1
        return self._pages[idx] if idx < len(self._pages) else None


def make_client(tmp_path):
    rate_limiter = RateLimiter(max_requests=1000, window_seconds=1)
    return SpotifyClient(
        client_id="dummy",
        client_secret="dummy",
        cache_path=tmp_path / "token_cache",
        rate_limiter=rate_limiter,
        open_browser=False,
    )


def _track(track_id):
    return {"id": track_id, "uri": f"spotify:track:{track_id}", "name": "Song", "artists": [], "duration_ms": 1000}


def test_get_playlist_tracks_accepts_item_key(tmp_path):
    """Regression test: the installed spotipy's playlist_items() hits
    /playlists/{id}/items, whose response nests each track under "item", not
    the older documented "track" key -- previously this silently dropped
    every track (reconcile always saw 0 tracks)."""
    client = make_client(tmp_path)
    client._sp = FakeRawSpotify([{"items": [{"item": _track("t1")}], "next": None}])

    tracks = client.get_playlist_tracks("pl1")

    assert [t["id"] for t in tracks] == ["t1"]


def test_get_playlist_tracks_still_accepts_track_key(tmp_path):
    client = make_client(tmp_path)
    client._sp = FakeRawSpotify([{"items": [{"track": _track("t1")}], "next": None}])

    tracks = client.get_playlist_tracks("pl1")

    assert [t["id"] for t in tracks] == ["t1"]


def test_get_playlist_tracks_paginates_across_item_key_pages(tmp_path):
    client = make_client(tmp_path)
    page1 = {"items": [{"item": _track("t1")}], "next": "page2"}
    page2 = {"items": [{"item": _track("t2")}], "next": None}
    client._sp = FakeRawSpotify([page1, page2])

    tracks = client.get_playlist_tracks("pl1")

    assert [t["id"] for t in tracks] == ["t1", "t2"]


def test_get_playlist_tracks_skips_items_with_neither_key(tmp_path):
    client = make_client(tmp_path)
    client._sp = FakeRawSpotify([{"items": [{"added_at": "2026-01-01"}], "next": None}])

    tracks = client.get_playlist_tracks("pl1")

    assert tracks == []
