"""Local library scanning, per Build Spec §4 and §9.

Expects the single configured music root laid out as
{Artist}/{Album}/{Title} - {Artists}.{ext}, scanned recursively.

Scanned on demand (rescan() is called right before a "skip local tracks"
check in queue_engine.py), not watched independently.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}


@dataclass(frozen=True)
class LocalTrack:
    path: Path
    artist: str
    album: str
    title: str
    artists: str


def parse_track_file(path: Path, music_root: Path) -> LocalTrack | None:
    """Parse {Artist}/{Album}/{Title} - {Artists}.{ext} relative to music_root."""
    try:
        rel = path.relative_to(music_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 3:
        return None
    artist_folder, album_folder = parts[0], parts[1]
    stem = path.stem
    if " - " in stem:
        title, _, artists = stem.partition(" - ")
    else:
        title, artists = stem, artist_folder
    return LocalTrack(
        path=path,
        artist=artist_folder,
        album=album_folder,
        title=title.strip(),
        artists=artists.strip(),
    )


def scan_music_root(music_root: Path | str) -> list[LocalTrack]:
    music_root = Path(music_root)
    if not music_root.exists():
        return []
    tracks = []
    for path in music_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            track = parse_track_file(path, music_root)
            if track is not None:
                tracks.append(track)
    return tracks


class LibraryIndex:
    """In-memory index of local tracks, refreshed by an explicit rescan()
    call rather than watching the filesystem itself."""

    def __init__(self, music_root: Path | str):
        self.music_root = Path(music_root)
        self._lock = threading.Lock()
        self._tracks: list[LocalTrack] = []

    def rescan(self) -> list[LocalTrack]:
        tracks = scan_music_root(self.music_root)
        with self._lock:
            self._tracks = tracks
        return tracks

    def tracks(self) -> list[LocalTrack]:
        with self._lock:
            return list(self._tracks)
