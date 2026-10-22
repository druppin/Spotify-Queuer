"""The app's one SQLite database: local files, Spotify playlists and tracks,
the links between them, queue history and Mixxx prep status.

Each component (library index, download watcher, catalog) opens its own
connection with connect() -- WAL mode lets the watcher thread, the queue
worker and the GUI read while one of them writes. Paths are stored
normalized (norm_path) so the library scan and the watcher agree on the key
for the same file.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("spotify_queuer.db")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    snapshot_id TEXT,                 -- NULL = tracks never fetched
    fetched_at  REAL
);
CREATE TABLE IF NOT EXISTS tracks (
    id      TEXT PRIMARY KEY,         -- Spotify track id
    uri     TEXT NOT NULL UNIQUE,
    title   TEXT NOT NULL,
    artists TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,     -- Spotify order; a playlist can hold a track twice
    track_id    TEXT NOT NULL REFERENCES tracks(id),
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX IF NOT EXISTS playlist_tracks_by_track ON playlist_tracks(track_id);

-- Every audio file the app knows about: library files (in_library = 1, parsed
-- columns set) and/or downloads the watcher has seen (seen_* set). The scan
-- and the watcher keep separate size/mtime so neither hides a change from the other.
CREATE TABLE IF NOT EXISTS library_files (
    path          TEXT PRIMARY KEY,
    size          INTEGER,
    mtime         REAL,
    artist_folder TEXT,
    album_folder  TEXT,
    title         TEXT,
    artists       TEXT,
    tag_title     TEXT,               -- NULL = tags never read
    tag_artists   TEXT,
    in_library    INTEGER NOT NULL DEFAULT 0,
    seen_size     INTEGER,
    seen_mtime    REAL,
    first_seen_at REAL NOT NULL,
    needs_prep    INTEGER NOT NULL DEFAULT 0   -- new download, tracked until Mixxx-ready
);

CREATE TABLE IF NOT EXISTS track_files (
    track_id  TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    path      TEXT NOT NULL REFERENCES library_files(path) ON DELETE CASCADE,
    source    TEXT NOT NULL CHECK (source IN ('download', 'match')),
    score     REAL,
    linked_at REAL NOT NULL,
    PRIMARY KEY (track_id, path)
);
CREATE INDEX IF NOT EXISTS track_files_by_path ON track_files(path);

CREATE TABLE IF NOT EXISTS queue_events (
    id          INTEGER PRIMARY KEY,
    track_id    TEXT NOT NULL REFERENCES tracks(id),
    playlist_id TEXT REFERENCES playlists(id) ON DELETE SET NULL,
    queued_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS queue_events_by_track ON queue_events(track_id, queued_at);

CREATE TABLE IF NOT EXISTS mixxx_status (
    path         TEXT PRIMARY KEY REFERENCES library_files(path) ON DELETE CASCADE,
    mixxx_id     INTEGER NOT NULL,
    hotcue_count INTEGER NOT NULL,
    rating       INTEGER NOT NULL,
    bpm_locked   INTEGER NOT NULL,
    synced_at    REAL NOT NULL
);
"""


def norm_path(path: Path | str) -> str:
    return os.path.normpath(str(path))


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the database (creating the schema if needed). Safe to share across
    threads only behind the caller's own lock, as every component does."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def ensure_migrated(db_path: Path | str, legacy_dir: Path | str) -> None:
    """One-time import of the pre-v1 per-feature databases in legacy_dir.
    The old files are only read, never modified, so they stay as a backup."""
    conn = connect(db_path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        legacy_dir = Path(legacy_dir)
        with conn:
            _import_library(conn, legacy_dir / "library_index.sqlite3")
            _import_seen_files(conn, legacy_dir / "download_watcher_state.sqlite3")
            _import_queue_history(conn, legacy_dir / "queue_history.sqlite3")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def _legacy_rows(path: Path, sql: str) -> list[tuple]:
    if not path.exists():
        return []
    legacy = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return legacy.execute(sql).fetchall()
    except sqlite3.OperationalError:
        logger.warning("Skipping legacy database %s: unexpected schema", path)
        return []
    finally:
        legacy.close()


def _import_library(conn: sqlite3.Connection, path: Path) -> None:
    rows = _legacy_rows(
        path, "SELECT path, size, mtime, artist, album, title, artists, tag_title, tag_artists FROM library_tracks"
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO library_files
            (path, size, mtime, artist_folder, album_folder, title, artists, tag_title, tag_artists,
             in_library, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        [(norm_path(r[0]), *r[1:], r[2]) for r in rows],
    )
    logger.info("Migrated %d library file(s)", len(rows))


def _import_seen_files(conn: sqlite3.Connection, path: Path) -> None:
    rows = _legacy_rows(path, "SELECT path, size, mtime, first_seen_date FROM seen_files")
    for raw_path, size, mtime, first_seen_date in rows:
        first_seen = datetime.fromisoformat(first_seen_date).timestamp()
        conn.execute(
            """
            INSERT INTO library_files (path, seen_size, seen_mtime, first_seen_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                seen_size = excluded.seen_size, seen_mtime = excluded.seen_mtime,
                first_seen_at = excluded.first_seen_at
            """,
            (norm_path(raw_path), size, mtime, first_seen),
        )
    logger.info("Migrated %d downloaded file(s)", len(rows))


def _import_queue_history(conn: sqlite3.Connection, path: Path) -> None:
    rows = _legacy_rows(
        path,
        "SELECT uri, title, artists, playlist_id, playlist_name, queued_at, downloaded_at, download_path"
        " FROM queued_tracks",
    )
    for uri, title, artists, playlist_id, playlist_name, queued_at, downloaded_at, download_path in rows:
        track_id = uri.rsplit(":", 1)[-1]
        conn.execute("INSERT OR IGNORE INTO tracks (id, uri, title, artists) VALUES (?, ?, ?, ?)",
                     (track_id, uri, title, artists))
        conn.execute("INSERT OR IGNORE INTO playlists (id, name) VALUES (?, ?)", (playlist_id, playlist_name))
        conn.execute("INSERT INTO queue_events (track_id, playlist_id, queued_at) VALUES (?, ?, ?)",
                     (track_id, playlist_id, queued_at))
        if download_path:
            # Linked only if the file is a known row -- the FK forbids dangling links.
            conn.execute(
                """
                INSERT OR IGNORE INTO track_files (track_id, path, source, linked_at)
                SELECT ?, path, 'download', ? FROM library_files WHERE path = ?
                """,
                (track_id, downloaded_at, norm_path(download_path)),
            )
    logger.info("Migrated %d queue history row(s)", len(rows))
