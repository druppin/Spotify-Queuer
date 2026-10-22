"""Persisted record of every track the app has sent to Spotify playback, so a
track queued yesterday (and maybe not downloaded yet) isn't queued again
today. The local-library check alone can't catch that: a queued track only
shows up in the library once its download lands and gets sorted in.

A pending (queued, not yet downloaded) track blocks re-queuing for
requeue_after_seconds -- long enough to cover a normal download, short
enough that a download that failed outright doesn't block the track forever.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


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

    def blocked_uris(self, requeue_after_seconds: float, now: float | None = None) -> set[str]:
        """URIs that must not be queued again: already downloaded, or queued
        within the last requeue_after_seconds and still pending."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT uri FROM queued_tracks WHERE downloaded_at IS NOT NULL OR queued_at > ?",
                (now - requeue_after_seconds,),
            ).fetchall()
        return {r[0] for r in rows}

    def record_queued(self, tracks, playlist_names: dict[str, str], now: float | None = None) -> None:
        """Record `tracks` (QueuedTrack) as queued now. Re-recording an
        expired pending track restarts its wait under its newest playlist."""
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
