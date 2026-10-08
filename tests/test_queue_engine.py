import pytest

from spotify_queuer.library_index import LibraryIndex
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
        self.started_playback = None  # (device_id, uris)

    def get_playlist_tracks(self, playlist_id):
        return self.playlists[playlist_id]

    def get_playback_state(self):
        return self.playback_state

    def get_devices(self):
        return self.devices

    def queue_track(self, uri):
        self.queued_uris.append(uri)

    def start_playback(self, device_id, uris):
        self.started_playback = (device_id, list(uris))


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


def test_queue_tracks_starts_playback_when_nothing_active():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = None

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=False)

    assert queued == 1
    assert spotify.started_playback == ("dev1", ["spotify:track:a"])
    assert spotify.queued_uris == []


def test_queue_tracks_appends_when_something_playing():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = {"is_playing": True, "device": {"id": "dev1"}}

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=False)

    assert queued == 1
    assert spotify.queued_uris == ["spotify:track:a"]
    assert spotify.started_playback is None


def test_queue_tracks_interrupt_playback_always_uses_start_playback():
    spotify = FakeSpotify()
    spotify.devices = [{"id": "dev1", "name": "My PC"}]
    spotify.playback_state = {"is_playing": True, "device": {"id": "dev1"}}

    from spotify_queuer.queue_engine import QueuedTrack

    tracks = [QueuedTrack(uri="spotify:track:a", title="A", artists="X")]
    queued = queue_tracks(spotify, "My PC", tracks, interrupt_playback=True)

    assert queued == 1
    assert spotify.started_playback == ("dev1", ["spotify:track:a"])


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
    assert spotify.started_playback == ("dev1", ["spotify:track:b"])
