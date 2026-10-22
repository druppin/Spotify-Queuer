import pytest

from spotify_queuer.library_index import LibraryIndex
from spotify_queuer.matching import MatchIndex
from spotify_queuer.queue_engine import (
    DeviceNotFoundError,
    collect_playlist_tracks,
    filter_not_local,
    queue_tracks,
    run_queue,
)


class FakeSpotify:
    def __init__(self):
        self.playlists = {}
        self.playback_state = None
        self.devices = []
        self.queued_uris = []
        self.started_context = None  # (device_id, context_uri)
        self.skips = 0

    def get_playlist_tracks(self, playlist_id):
        return self.playlists[playlist_id]

    def get_playback_state(self):
        return self.playback_state

    def get_devices(self):
        return self.devices

    def queue_track(self, uri):
        self.queued_uris.append(uri)

    def start_context(self, device_id, context_uri):
        self.started_context = (device_id, context_uri)
        name = next(d["name"] for d in self.devices if d["id"] == device_id)
        self.playback_state = {"is_playing": True, "device": {"id": device_id, "name": name}, "item": {"uri": "x"}}

    def next_track(self, device_id):
        self.skips += 1


def make_track(track_id, name, artists):
    return {
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "name": name,
        "artists": [{"name": a} for a in artists],
    }


def test_collect_playlist_tracks_concatenates_in_order():
    spotify = FakeSpotify()
    spotify.playlists = {
        "p1": [make_track("a", "Song A", ["Artist 1"])],
        "p2": [make_track("b", "Song B", ["Artist 2"])],
    }
    tracks = collect_playlist_tracks(spotify, ["p1", "p2"])
    assert [t.uri for t in tracks] == ["spotify:track:a", "spotify:track:b"]


def test_collect_playlist_tracks_skips_tracks_without_uri():
    spotify = FakeSpotify()
    spotify.playlists = {"p1": [{"id": "a", "uri": None, "name": "Local file", "artists": []}]}
    tracks = collect_playlist_tracks(spotify, ["p1"])
    assert tracks == []


def test_filter_not_local_drops_matching_local_files(tmp_path):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "Song A - Artist 1.mp3").write_bytes(b"x")
    library = LibraryIndex(tmp_path)
    library.rescan()

    spotify = FakeSpotify()
    spotify.playlists = {
        "p1": [
            make_track("a", "Song A", ["Artist 1"]),
            make_track("b", "Song B", ["Artist 2"]),
        ]
    }
    tracks = collect_playlist_tracks(spotify, ["p1"])
    kept = filter_not_local(tracks, library, threshold=87)

    assert [t.uri for t in kept] == ["spotify:track:b"]


def test_queue_tracks_starts_source_playlist_when_nothing_active_then_skips_to_queue():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = None

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X", playlist_id="p1")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=False)

    assert queued == 1
    assert spotify.started_context == ("dev1", "spotify:playlist:p1")
    assert spotify.queued_uris == ["spotify:track:a"]
    assert spotify.skips == 1  # off the playlist's first track, onto the queued one


def test_queue_tracks_appends_when_something_playing():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = {"is_playing": True, "device": {"id": "dev1"}}

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X", playlist_id="p1")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=False)

    assert queued == 1
    assert spotify.queued_uris == ["spotify:track:a"]
    assert spotify.started_context is None
    assert spotify.skips == 0


def test_queue_tracks_interrupt_skips_to_the_queued_tracks():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = {"is_playing": True, "device": {"id": "dev1"}}

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X", playlist_id="p1")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=True)

    assert queued == 1
    assert spotify.queued_uris == ["spotify:track:a"]
    assert spotify.started_context is None
    assert spotify.skips == 1


def test_queue_tracks_raises_when_device_missing():
    spotify = FakeSpotify()
    spotify.devices = []

    from spotify_queuer.queue_engine import QueuedTrack

    with pytest.raises(DeviceNotFoundError):
        queue_tracks(spotify, "Missing Device", [QueuedTrack("spotify:track:a", "A", "X")])


def test_queue_tracks_reports_progress_per_track_when_appending():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = {"is_playing": True, "device": {"id": "dev1"}}

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [
        QueuedTrack(uri="spotify:track:a", title="A", artists="X"),
        QueuedTrack(uri="spotify:track:b", title="B", artists="Y"),
    ]
    messages = []
    queue_tracks(spotify, "My PC", tracks, interrupt_playback=False, on_progress=messages.append)

    assert len(messages) == 2
    assert "1/2" in messages[0] and "A" in messages[0]
    assert "2/2" in messages[1] and "B" in messages[1]


def test_run_queue_end_to_end(tmp_path):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "Song A - Artist 1.mp3").write_bytes(b"x")
    library = LibraryIndex(tmp_path)

    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = None
    spotify.playlists = {
        "p1": [
            make_track("a", "Song A", ["Artist 1"]),  # already local -- should be skipped
            make_track("b", "Song B", ["Artist 2"]),
        ]
    }

    result = run_queue(
        spotify, library, "My PC", ["p1"], skip_local=True, interrupt_playback=False, fuzzy_threshold=87
    )

    assert result.total_tracks == 2
    assert result.skipped_local == 1
    assert result.queued == 1
    assert spotify.queued_uris == ["spotify:track:b"]


def _queue_setup(tmp_path):
    from spotify_queuer.catalog import Catalog

    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playlists = {
        "p1": [make_track("a", "Song A", ["Artist 1"]), make_track("b", "Song B", ["Artist 2"])],
        "p2": [make_track("b", "Song B", ["Artist 2"]), make_track("c", "Song C", ["Artist 3"])],
    }
    db = tmp_path / "app.sqlite3"
    library = LibraryIndex(tmp_path / "music", db_path=db)
    catalog = Catalog(db)
    catalog.sync_playlists([{"id": "p1", "name": "Mix One"}, {"id": "p2", "name": "Mix Two"}])
    return spotify, library, catalog


def _run(spotify, library, catalog, playlist_ids, snapshots=None):
    return run_queue(
        spotify, library, "My PC", playlist_ids, skip_local=True, interrupt_playback=True, fuzzy_threshold=87,
        catalog=catalog, snapshots=snapshots,
    )


def test_run_queue_queues_track_on_two_playlists_once(tmp_path):
    spotify, library, catalog = _queue_setup(tmp_path)
    result = _run(spotify, library, catalog, ["p1", "p2"])
    assert spotify.queued_uris[-3:] == ["spotify:track:a", "spotify:track:b", "spotify:track:c"]
    assert result.skipped_duplicate == 1
    row = catalog._conn.execute("SELECT playlist_id FROM queue_events WHERE track_id = 'b'").fetchone()
    assert row == ("p1",)  # first playlist in queue order wins


def test_run_queue_requeues_tracks_an_earlier_run_sent_but_never_downloaded(tmp_path):
    # Spotify's queue is volatile (replaced by the next start_playback, dropped
    # when the session ends), so an earlier run's tracks must not be skipped.
    spotify, library, catalog = _queue_setup(tmp_path)
    _run(spotify, library, catalog, ["p1"])
    result = _run(spotify, library, catalog, ["p1"])
    assert result.queued == 2


def test_run_queue_reads_unchanged_playlists_from_the_database(tmp_path):
    spotify, library, catalog = _queue_setup(tmp_path)
    fetches = []
    original = spotify.get_playlist_tracks
    spotify.get_playlist_tracks = lambda pid: fetches.append(pid) or original(pid)

    _run(spotify, library, catalog, ["p1", "p2"], snapshots={"p1": "s1", "p2": "s1"})
    result = _run(spotify, library, catalog, ["p1", "p2"], snapshots={"p1": "s1", "p2": "s2"})

    assert fetches == ["p1", "p2", "p2"]  # p1 unchanged on the second run, p2's snapshot moved
    assert result.cached_playlists == 1
    assert spotify.queued_uris[-3:] == ["spotify:track:a", "spotify:track:b", "spotify:track:c"]


def test_run_queue_without_snapshot_always_fetches(tmp_path):
    spotify, library, catalog = _queue_setup(tmp_path)
    _run(spotify, library, catalog, ["p1"])
    assert _run(spotify, library, catalog, ["p1"]).cached_playlists == 0


def test_run_queue_links_local_matches_and_trusts_the_link_next_time(tmp_path):
    spotify, library, catalog = _queue_setup(tmp_path)
    song = tmp_path / "music" / "Artist 1" / "Album" / "Song A - Artist 1.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")

    first = _run(spotify, library, catalog, ["p1"])
    assert first.skipped_local == 1
    assert catalog.linked_uris(["spotify:track:a"]) == {"spotify:track:a"}

    library._match_index = MatchIndex([])  # no fuzzy candidates: only the stored link can skip "a"
    assert _run(spotify, library, catalog, ["p1"]).skipped_local == 1


def test_deleted_file_drops_its_link_on_rescan(tmp_path):
    spotify, library, catalog = _queue_setup(tmp_path)
    song = tmp_path / "music" / "Artist 1" / "Album" / "Song A - Artist 1.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    _run(spotify, library, catalog, ["p1"])

    song.unlink()
    library.rescan()
    assert catalog.linked_uris(["spotify:track:a"]) == set()


def test_verify_playback_raises_when_device_accepts_but_plays_nothing(monkeypatch):
    from spotify_queuer import queue_engine
    from spotify_queuer.queue_engine import PlaybackNotStartedError, verify_playback

    monkeypatch.setattr(queue_engine.time, "sleep", lambda s: None)
    spotify = FakeSpotify()
    # What the stuck desktop client reported: active device, nothing loaded.
    spotify.playback_state = {"is_playing": False, "device": {"id": "dev1", "name": "My PC"}, "item": None}
    with pytest.raises(PlaybackNotStartedError):
        verify_playback(spotify, "My PC", expect_playing=True)
    with pytest.raises(PlaybackNotStartedError):
        verify_playback(spotify, "My PC", expect_playing=False)


def test_verify_playback_accepts_paused_device_when_appending():
    from spotify_queuer.queue_engine import verify_playback

    spotify = FakeSpotify()
    spotify.playback_state = {"is_playing": False, "device": {"id": "dev1", "name": "My PC"}, "item": {"uri": "x"}}
    verify_playback(spotify, "My PC", expect_playing=False)  # paused on purpose; tracks wait behind
