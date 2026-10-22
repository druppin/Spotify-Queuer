"""Builds a track list from selected Spotify playlists and sends it to
playback -- the core of the "pick playlists on the left, queue them" feature.

No batch queue endpoint exists in the Spotify Web API, so every track costs
one add-to-queue request (queue_track). Starting playback with a list of
track URIs would be one request, but the Spotify desktop client accepts that
call and then plays nothing (verified 2026-10-22: a playlist context and
add-to-queue both work on the same device), so it isn't used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .catalog import Catalog
from .library_index import LibraryIndex
from .spotify_client import SpotifyClient

logger = logging.getLogger("spotify_queuer.queue_engine")


@dataclass(frozen=True)
class QueuedTrack:
    uri: str
    title: str
    artists: str  # display string ("Artist A, Artist B"), used for fuzzy matching
    playlist_id: str = ""  # the selected playlist this track was collected from


@dataclass(frozen=True)
class QueueResult:
    total_tracks: int
    skipped_local: int
    queued: int
    skipped_duplicate: int = 0  # same track on more than one selected playlist
    cached_playlists: int = 0  # read from the database: snapshot unchanged, no Spotify request


class DeviceNotFoundError(RuntimeError):
    pass


def _track_from_raw(raw: dict, playlist_id: str = "") -> QueuedTrack | None:
    if not raw or not raw.get("uri"):
        return None
    artists = ", ".join(a.get("name", "") for a in raw.get("artists", []) if a.get("name"))
    return QueuedTrack(uri=raw["uri"], title=raw.get("name", "") or "", artists=artists, playlist_id=playlist_id)


def collect_playlist_tracks(
    spotify: SpotifyClient,
    playlist_ids: list[str],
    on_progress=None,
    catalog: Catalog | None = None,
    snapshots: dict[str, str] | None = None,
) -> list[QueuedTrack]:
    """Tracks for each playlist ID, concatenated in the given order.

    With a catalog, a playlist whose current snapshot_id (from `snapshots`,
    as listed by Spotify) matches the stored one is read from the database
    at no request cost; otherwise its items are fetched (one request per 100
    tracks) and stored. on_progress(message), if given, is called before
    each playlist so a caller can show live status during what can be a
    slow, rate-limited series of requests.
    """
    snapshots = snapshots or {}
    tracks: list[QueuedTrack] = []
    for i, playlist_id in enumerate(playlist_ids, start=1):
        snapshot_id = snapshots.get(playlist_id)
        if catalog is not None and catalog.is_current(playlist_id, snapshot_id):
            tracks.extend(QueuedTrack(*row, playlist_id=playlist_id) for row in catalog.cached_tracks(playlist_id))
            continue
        if on_progress is not None:
            on_progress(f"Fetching tracks for playlist {i}/{len(playlist_ids)}...")
        fetched = [t for t in (_track_from_raw(raw, playlist_id) for raw in spotify.get_playlist_tracks(playlist_id)) if t]
        if catalog is not None:
            catalog.store_playlist(playlist_id, snapshot_id, fetched)
        tracks.extend(fetched)
    return tracks


def filter_not_local(
    tracks: list[QueuedTrack], library: LibraryIndex, threshold: int, on_progress=None, catalog: Catalog | None = None
) -> list[QueuedTrack]:
    """Drop tracks that already have a fuzzy-matching file in the local
    library. Pure local computation (already-scanned file list + in-memory
    fuzzy comparisons) -- costs zero extra Spotify requests. With a catalog,
    tracks already linked to a file skip matching, and new matches are
    linked so the next run (and the named playlist files) can use them."""
    if on_progress is not None:
        on_progress(f"Checking {len(tracks)} track(s) against your local library...")
    linked = catalog.linked_uris([t.uri for t in tracks]) if catalog is not None else set()
    index = library.match_index()
    kept = []
    for track in tracks:
        if track.uri in linked:
            continue
        candidate, result = index.best(track.title, track.artists, threshold=threshold)
        if candidate is None:
            kept.append(track)
        elif catalog is not None:
            catalog.link(track.uri, candidate.path, "match", result.score)
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
    """Add `tracks` to Spotify's queue, one request per track, in order.

    Nothing active on the device: the first track's source playlist is
    started first, since the queue needs something playing to sit behind.
    interrupt_playback=True (or that fresh start): after queuing, skip to the
    first queued track. Otherwise the tracks wait behind what's playing.
    on_progress(message), if given, is called before each add-to-queue
    request, since that loop is the slowest part of queuing a big playlist.
    """
    if not tracks:
        return 0

    device_id = resolve_device_id(spotify, device_name)
    started = False
    if spotify.get_playback_state() is None:
        playlist_id = next((t.playlist_id for t in tracks if t.playlist_id), None)
        if playlist_id:
            if on_progress is not None:
                on_progress("Starting playback...")
            spotify.start_context(device_id, f"spotify:playlist:{playlist_id}")
            started = True

    for i, track in enumerate(tracks, start=1):
        if on_progress is not None:
            on_progress(f"Queuing track {i}/{len(tracks)}: {track.title}")
        spotify.queue_track(track.uri)

    if interrupt_playback or started:
        spotify.next_track(device_id)  # leave whatever's playing for the first queued track
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
    catalog: Catalog | None = None,
    snapshots: dict[str, str] | None = None,
    library_max_age_seconds: float = 300,
) -> QueueResult:
    cached_playlists = sum(
        catalog is not None and catalog.is_current(pid, (snapshots or {}).get(pid)) for pid in playlist_ids
    )
    tracks = collect_playlist_tracks(spotify, playlist_ids, on_progress=on_progress, catalog=catalog, snapshots=snapshots)
    total = len(tracks)

    # First occurrence wins, so a track on two playlists belongs to the earlier one in queue order.
    seen: set[str] = set()
    unique = []
    for track in tracks:
        if track.uri not in seen:
            seen.add(track.uri)
            unique.append(track)
    skipped_duplicate = total - len(unique)
    tracks = unique

    skipped_local = 0
    if skip_local:
        if on_progress is not None:
            on_progress("Scanning local library...")
        library.rescan_if_older_than(library_max_age_seconds)
        before = len(tracks)
        tracks = filter_not_local(tracks, library, fuzzy_threshold, on_progress=on_progress, catalog=catalog)
        skipped_local = before - len(tracks)

    queued = queue_tracks(spotify, device_name, tracks, interrupt_playback, on_progress=on_progress)
    # Recorded only so downloads can be filed under their source playlist -- not
    # used to skip tracks: Spotify's queue can be cleared or replaced and is
    # dropped when the session ends, so "sent to Spotify" proves nothing.
    if catalog is not None and queued:
        catalog.record_queued(tracks)
    logger.info(
        "Queued %d/%d track(s) from %d playlist(s), %d cached / %d fetched (%d already local, %d duplicate, skipped)",
        queued,
        total,
        len(playlist_ids),
        cached_playlists,
        len(playlist_ids) - cached_playlists,
        skipped_local,
        skipped_duplicate,
    )
    return QueueResult(
        total_tracks=total,
        skipped_local=skipped_local,
        queued=queued,
        skipped_duplicate=skipped_duplicate,
        cached_playlists=cached_playlists,
    )
