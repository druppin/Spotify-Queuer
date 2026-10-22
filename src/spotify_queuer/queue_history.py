"""Persisted record of every track the app has sent to Spotify playback and
which selected playlist it came from, so the download watcher can file a new
download under `<source playlist name>.m3u8`."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .matching import MatchIndex


class QueueHistory:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queued_tracks (
                uri TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artists TEXT NOT NULL,
                playlist_id TEXT NOT NULL,
                playlist_name TEXT NOT NULL,
                queued_at REAL NOT NULL,
                downloaded_at REAL,
                download_path TEXT
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record_queued(self, tracks, playlist_names: dict[str, str], now: float | None = None) -> None:
        """Record `tracks` (QueuedTrack) as queued now, under their newest playlist."""
        now = time.time() if now is None else now
        rows = [
            (t.uri, t.title, t.artists, t.playlist_id, playlist_names.get(t.playlist_id, t.playlist_id), now)
            for t in tracks
        ]
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO queued_tracks (uri, title, artists, playlist_id, playlist_name, queued_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(uri) DO UPDATE SET
                    playlist_id = excluded.playlist_id,
                    playlist_name = excluded.playlist_name,
                    queued_at = excluded.queued_at
                """,
                rows,
            )
            self._conn.commit()

    def match_download(
        self, path: Path, keys: list[tuple[str, str]], threshold: int, now: float | None = None
    ) -> tuple[str, str] | None:
        """Fuzzy-match a newly downloaded file, by its (title, artists) keys,
        against tracks still awaiting download. On a match, marks that track
        downloaded and returns its (playlist_id, playlist_name)."""
        now = time.time() if now is None else now
        with self._lock:
            pending = self._conn.execute(
                "SELECT uri, title, artists, playlist_id, playlist_name FROM queued_tracks WHERE downloaded_at IS NULL"
            ).fetchall()
            index = MatchIndex(pending, keys=lambda row: [(row[1], row[2])])
            for title, artists in keys:
                if not title:
                    continue
                row, _ = index.best(title, artists, threshold=threshold)
                if row is not None:
                    self._conn.execute(
                        "UPDATE queued_tracks SET downloaded_at = ?, download_path = ? WHERE uri = ?",
                        (now, str(path), row[0]),
                    )
                    self._conn.commit()
                    return row[3], row[4]
        return None

    def downloads_for_playlist(self, playlist_id: str) -> list[tuple[Path, str, str]]:
        """(download_path, title, artists) for every downloaded track filed under playlist_id, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT download_path, title, artists FROM queued_tracks"
                " WHERE playlist_id = ? AND download_path IS NOT NULL ORDER BY downloaded_at",
                (playlist_id,),
            ).fetchall()
        return [(Path(r[0]), r[1], r[2]) for r in rows]
