"""Local library scanning, per Build Spec §4 and §9.

Expects the single configured music root laid out as
{Artist}/{Album}/{Title} - {Artists}.{ext}, scanned recursively. Files are
matched on their filename AND their title/artist tags (when present):
Spotify titles often contain " - " themselves ("Song - Extended Mix"), so
the filename alone is ambiguous.

Scanned on demand (rescan_if_older_than() runs right before a "skip local tracks"
check in queue_engine.py), not watched independently. Rescans are
incremental: given a db_path, every known file is persisted in SQLite with
its size and mtime, so a rescan only lists directories and re-parses the
files that are new or changed since the last scan (including the last app
run), and the fuzzy-match index is only rebuilt when something actually
changed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError

from .matching import MatchIndex

logger = logging.getLogger("spotify_queuer.library_index")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}


# Joins multi-valued artist tags; split back apart by matching.split_artists.
TAG_ARTIST_SEPARATOR = "; "


@dataclass(frozen=True)
class LocalTrack:
    path: Path
    artist: str
    album: str
    title: str
    artists: str
    tag_title: str = ""
    tag_artists: str = ""

    def match_keys(self) -> list[tuple[str, str]]:
        """(title, artists) pairs this file can be recognized by: the parsed
        filename, the whole filename stem (for titles that contain " - "),
        and the tags."""
        keys = [(self.title, self.artists)]
        stem = self.path.stem
        if stem != self.title:
            keys.append((stem, self.tag_artists or self.artist))
        if self.tag_title:
            keys.append((self.tag_title, self.tag_artists or self.artist))
        return list(dict.fromkeys(keys))


def read_tags(path: Path) -> tuple[str, str]:
    """(title, artists) from the file's tags; empty strings when missing or unreadable."""
    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError) as exc:
        logger.debug("Could not read tags from %s: %s", path, exc)
        return "", ""
    if audio is None or not audio.tags:
        return "", ""
    try:
        titles = audio.tags.get("title") or []
        artists = audio.tags.get("artist") or []
    except (KeyError, ValueError):
        return "", ""
    title = str(titles[0]).strip() if titles else ""
    return title, TAG_ARTIST_SEPARATOR.join(str(a).strip() for a in artists if str(a).strip())


def parse_track_file(path: Path, music_root: Path, with_tags: bool = True) -> LocalTrack | None:
    """Parse {Artist}/{Album}/{Title} - {Artists}.{ext} relative to music_root,
    plus the file's title/artist tags unless with_tags is False."""
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
    tag_title, tag_artists = read_tags(path) if with_tags else ("", "")
    return LocalTrack(
        path=path,
        artist=artist_folder,
        album=album_folder,
        title=title.strip(),
        artists=artists.strip(),
        tag_title=tag_title,
        tag_artists=tag_artists,
    )


def _walk_audio_files(root: Path):
    """Yield (path_str, size, mtime) for every audio file under root.

    os.scandir rather than Path.rglob + stat: on Windows the directory
    listing already carries size/mtime, so this costs one listing per folder
    instead of an extra stat call per file.
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS and entry.is_file():
                            st = entry.stat()
                            yield entry.path, st.st_size, st.st_mtime
                    except OSError:
                        continue  # vanished or unreadable mid-scan; the next rescan will pick it up
        except OSError:
            logger.warning("Could not list folder during library scan: %s", current)


def scan_music_root(music_root: Path | str) -> list[LocalTrack]:
    music_root = Path(music_root)
    if not music_root.exists():
        return []
    tracks = []
    for path_str, _size, _mtime in _walk_audio_files(music_root):
        track = parse_track_file(Path(path_str), music_root)
        if track is not None:
            tracks.append(track)
    return tracks


class _LibraryDB:
    """Persisted (path, size, mtime) -> parsed track rows."""

    _TAG_COLUMNS = ("tag_title", "tag_artists")

    def __init__(self, db_path: Path | str):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_tracks (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                title TEXT NOT NULL,
                artists TEXT NOT NULL,
                tag_title TEXT,
                tag_artists TEXT
            )
            """
        )
        # DBs from before tags were indexed: add the columns, left NULL so load() re-reads those files.
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(library_tracks)")}
        for column in self._TAG_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE library_tracks ADD COLUMN {column} TEXT")
        self._conn.commit()

    def load(self) -> dict[str, tuple[int, float, LocalTrack | None]]:
        rows = self._conn.execute(
            "SELECT path, size, mtime, artist, album, title, artists, tag_title, tag_artists FROM library_tracks"
        ).fetchall()
        entries = {}
        for path, size, mtime, artist, album, title, artists, tag_title, tag_artists in rows:
            if tag_title is None:
                mtime = -1.0  # tags never read for this row; forces a re-parse on the next rescan
            track = LocalTrack(Path(path), artist, album, title, artists, tag_title or "", tag_artists or "")
            entries[path] = (size, mtime, track)
        return entries

    def apply_changes(self, upserts: list[tuple[str, int, float, LocalTrack]], deleted: list[str]) -> None:
        with self._conn:
            self._conn.executemany("DELETE FROM library_tracks WHERE path = ?", [(p,) for p in deleted])
            self._conn.executemany(
                """
                INSERT INTO library_tracks (path, size, mtime, artist, album, title, artists, tag_title, tag_artists)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime, artist = excluded.artist,
                    album = excluded.album, title = excluded.title, artists = excluded.artists,
                    tag_title = excluded.tag_title, tag_artists = excluded.tag_artists
                """,
                [
                    (p, size, mtime, t.artist, t.album, t.title, t.artists, t.tag_title, t.tag_artists)
                    for p, size, mtime, t in upserts
                ],
            )

    def close(self) -> None:
        self._conn.close()


class LibraryIndex:
    """Index of local tracks, refreshed by an explicit rescan() call rather
    than watching the filesystem itself. Optionally backed by SQLite
    (db_path) so the index survives restarts and rescans are incremental."""

    def __init__(self, music_root: Path | str, db_path: Path | str | None = None):
        self.music_root = Path(music_root)
        self._lock = threading.Lock()
        self._db = _LibraryDB(db_path) if db_path is not None else None
        # path -> (size, mtime, track); track is None for audio files outside
        # the {Artist}/{Album}/ layout, remembered so they aren't re-parsed every scan.
        self._entries: dict[str, tuple[int, float, LocalTrack | None]] = self._db.load() if self._db else {}
        self._match_index: MatchIndex | None = None
        self._last_scan: float | None = None

    def rescan_if_older_than(self, max_age_seconds: float) -> None:
        """Rescan unless the last scan finished within max_age_seconds -- the
        walk is the slow part of queuing, and back-to-back queue runs rarely
        see new library files in between."""
        if self._last_scan is None or time.monotonic() - self._last_scan > max_age_seconds:
            self.rescan()

    def rescan(self) -> list[LocalTrack]:
        if not self.music_root.exists():
            found = {}
        else:
            found = {path: (size, mtime) for path, size, mtime in _walk_audio_files(self.music_root)}

        with self._lock:
            old = self._entries
            new_entries: dict[str, tuple[int, float, LocalTrack | None]] = {}
            upserts = []
            for path, (size, mtime) in found.items():
                previous = old.get(path)
                if previous is not None and previous[0] == size and previous[1] == mtime:
                    new_entries[path] = previous
                    continue
                track = parse_track_file(Path(path), self.music_root)
                new_entries[path] = (size, mtime, track)
                if track is not None:
                    upserts.append((path, size, mtime, track))
            deleted = [path for path in old if path not in new_entries]
            # Files that didn't parse as tracks were never persisted, so only real tracks need deleting.
            deleted_in_db = [path for path in deleted if old[path][2] is not None]

            self._entries = new_entries
            if upserts or deleted_in_db:
                self._match_index = None
                if self._db is not None:
                    self._db.apply_changes(upserts, deleted_in_db)
            self._last_scan = time.monotonic()
            logger.info(
                "Library scan: %d file(s), %d new/changed, %d removed", len(new_entries), len(upserts), len(deleted)
            )
            return self._tracks_locked()

    def tracks(self) -> list[LocalTrack]:
        with self._lock:
            return self._tracks_locked()

    def match_index(self) -> MatchIndex:
        """Fuzzy-match index over the current tracks, cached until a rescan changes something."""
        with self._lock:
            if self._match_index is None:
                self._match_index = MatchIndex(self._tracks_locked(), keys=LocalTrack.match_keys)
            return self._match_index

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _tracks_locked(self) -> list[LocalTrack]:
        return [entry[2] for entry in self._entries.values() if entry[2] is not None]
