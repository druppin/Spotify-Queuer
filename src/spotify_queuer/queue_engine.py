"""Builds a track list from selected Spotify playlists and sends it to
playback -- the core of the "pick playlists on the left, queue them" feature.

No batch queue endpoint exists in the Spotify Web API: appending behind
whatever's currently playing costs one request per track (queue_track), no
way around it. Replacing playback outright (start_playback) costs exactly
one request for the whole batch, at the cost of interrupting what's playing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .library_index import LibraryIndex
from .matching import best_match
from .spotify_client import SpotifyClient

logger = logging.getLogger("spotify_queuer.queue_engine")


@dataclass(frozen=True)
class QueuedTrack:
    uri: str
    title: str
    artists: str  # display string ("Artist A, Artist B"), used for fuzzy matching


@dataclass(frozen=True)
class QueueResult:
    total_tracks: int
    skipped_local: int
    queued: int


class DeviceNotFoundError(RuntimeError):
    pass


def _track_from_raw(raw: dict) -> QueuedTrack | None:
    if not raw or not raw.get("uri"):
        return None
    artists = ", ".join(a.get("name", "") for a in raw.get("artists", []) if a.get("name"))
    return QueuedTrack(uri=raw["uri"], title=raw.get("name", "") or "", artists=artists)


def collect_playlist_tracks(
    spotify: SpotifyClient, playlist_ids: list[str], on_progress=None
) -> list[QueuedTrack]:
    """Fetch and concatenate tracks for each playlist ID, in the given order.

    One playlist-items fetch per playlist -- required regardless, since
    queuing needs the actual track URIs. on_progress(message), if given, is
    called before each playlist's fetch so a caller can show live status
    during what can be a slow, rate-limited series of requests.
    """
    tracks: list[QueuedTrack] = []
    for i, playlist_id in enumerate(playlist_ids, start=1):
        if on_progress is not None:
            on_progress(f"Fetching tracks for playlist {i}/{len(playlist_ids)}...")
        for raw in spotify.get_playlist_tracks(playlist_id):
            track = _track_from_raw(raw)
            if track is not None:
                tracks.append(track)
    return tracks


def filter_not_local(
    tracks: list[QueuedTrack], library: LibraryIndex, threshold: int, on_progress=None
) -> list[QueuedTrack]:
    """Drop tracks that already have a fuzzy-matching file in the local
    library. Pure local computation (already-scanned file list + in-memory
    fuzzy comparisons) -- costs zero extra Spotify requests."""
    if on_progress is not None:
        on_progress(f"Checking {len(tracks)} track(s) against your local library...")
    local_tracks = library.tracks()
    kept = []
    for track in tracks:
        candidate, _ = best_match(track.title, track.artists, local_tracks, threshold=threshold)
        if candidate is None:
            kept.append(track)
    return kept


def resolve_device_id(spotify: SpotifyClient, device_name: str) -> str:
    devices = spotify.get_devices()
    device = next((d for d in devices if d.get("name") == device_name), None)
    if device is None:
        raise DeviceNotFoundError(
            f"Spotify device '{device_name}' not found. Is Spotify open on that device?"
        )
    return device["id"]


def queue_tracks(
    spotify: SpotifyClient,
    device_name: str,
    tracks: list[QueuedTrack],
    interrupt_playback: bool = False,
    on_progress=None,
) -> int:
    """Send `tracks` to Spotify playback per the app's queue policy.

    Nothing currently active, or interrupt_playback=True: one start_playback
    request replaces/starts playback with the whole batch. Otherwise: one
    queue_track request per track, appended behind what's already playing --
    on_progress(message), if given, is called before each of those individual
    requests, since that loop is the slowest part of queuing a big playlist.
    """
    if not tracks:
        return 0

    device_id = resolve_device_id(spotify, device_name)
    state = spotify.get_playback_state()

    if interrupt_playback or state is None:
        if on_progress is not None:
            on_progress(f"Starting playback with {len(tracks)} track(s)...")
        spotify.start_playback(device_id, uris=[t.uri for t in tracks])
        return len(tracks)

    for i, track in enumerate(tracks, start=1):
        if on_progress is not None:
            on_progress(f"Queuing track {i}/{len(tracks)}: {track.title}")
        spotify.queue_track(track.uri)
    return len(tracks)


def run_queue(
    spotify: SpotifyClient,
    library: LibraryIndex,
    device_name: str,
    playlist_ids: list[str],
    skip_local: bool,
    interrupt_playback: bool,
    fuzzy_threshold: int,
    on_progress=None,
) -> QueueResult:
    tracks = collect_playlist_tracks(spotify, playlist_ids, on_progress=on_progress)
    total = len(tracks)

    if skip_local:
        if on_progress is not None:
            on_progress("Scanning local library...")
        library.rescan()
        tracks = filter_not_local(tracks, library, fuzzy_threshold, on_progress=on_progress)

    skipped = total - len(tracks)
    queued = queue_tracks(spotify, device_name, tracks, interrupt_playback, on_progress=on_progress)
    logger.info(
        "Queued %d/%d track(s) from %d playlist(s) (%d already local, skipped)",
        queued,
        total,
        len(playlist_ids),
        skipped,
    )
    return QueueResult(total_tracks=total, skipped_local=skipped, queued=queued)
