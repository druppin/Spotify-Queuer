"""Spotify side of the database (db.py): playlists cached by snapshot_id,
their tracks, which local file each track is (track_files), and queue
history. Lets an unchanged playlist be read without any Spotify request and
an already-matched track skip fuzzy matching.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .db import connect, norm_path
from .m3u_writer import M3UEntry, write_playlist_m3u
from .matching import MatchIndex

logger = logging.getLogger("spotify_queuer.catalog")

# A download is matched against tracks queued within this window.
DOWNLOAD_MATCH_WINDOW_SECONDS = 14 * 24 * 3600


def track_id_for_uri(uri: str) -> str:
    """Spotify track id for spotify:track:<id>; the whole uri for anything
    else (local files, episodes), which has no stable id of its own."""
    prefix = "spotify:track:"
    return uri[len(prefix):] if uri.startswith(prefix) else uri


class Catalog:
    def __init__(self, db_path: Path | str):
        self._conn = connect(db_path)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # -- playlists --
    def sync_playlists(self, playlists: list[dict]) -> None:
        """Record names for playlists as listed by Spotify. Leaves the stored
        snapshot_id alone: it marks what playlist_tracks holds, not what's current."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO playlists (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                [(p["id"], p.get("name") or p["id"]) for p in playlists],
            )

    def is_current(self, playlist_id: str, snapshot_id: str | None) -> bool:
        if not snapshot_id:
            return False
        with self._lock:
            row = self._conn.execute("SELECT snapshot_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        return row is not None and row[0] == snapshot_id

    def cached_tracks(self, playlist_id: str) -> list[tuple[str, str, str, bool]]:
        """(uri, title, artists, playable) in playlist order."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT t.uri, t.title, t.artists, COALESCE(t.is_playable, 1) = 1
                FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
                WHERE pt.playlist_id = ? ORDER BY pt.position
                """,
                (playlist_id,),
            ).fetchall()

    def store_playlist(self, playlist_id: str, snapshot_id: str | None, tracks) -> None:
        """Replace the stored track list for playlist_id with `tracks` (QueuedTrack-like)."""
        with self._lock, self._conn:
            self._upsert_tracks(tracks)
            self._conn.execute("INSERT OR IGNORE INTO playlists (id, name) VALUES (?, ?)", (playlist_id, playlist_id))
            self._conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
            self._conn.executemany(
                "INSERT INTO playlist_tracks (playlist_id, position, track_id) VALUES (?, ?, ?)",
                [(playlist_id, i, track_id_for_uri(t.uri)) for i, t in enumerate(tracks)],
            )
            self._conn.execute(
                "UPDATE playlists SET snapshot_id = ?, fetched_at = ? WHERE id = ?",
                (snapshot_id, time.time(), playlist_id),
            )

    def _upsert_tracks(self, tracks) -> None:
        self._conn.executemany(
            """
            INSERT INTO tracks (id, uri, title, artists, is_playable) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title, artists = excluded.artists, is_playable = excluded.is_playable
            """,
            [(track_id_for_uri(t.uri), t.uri, t.title, t.artists, int(t.playable)) for t in tracks],
        )

    # -- track <-> local file links --
    def linked_uris(self, uris: list[str]) -> set[str]:
        """The subset of `uris` already linked to a local file."""
        with self._lock:
            linked = {
                r[0] for r in self._conn.execute("SELECT DISTINCT t.uri FROM track_files f JOIN tracks t ON t.id = f.track_id")
            }
        return linked & set(uris)

    def link(self, uri: str, path: Path | str, source: str, score: float | None = None) -> bool:
        """Link a track to a local file; False if the file isn't a known library_files row."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO track_files (track_id, path, source, score, linked_at)
                SELECT ?, path, ?, ?, ? FROM library_files WHERE path = ?
                """,
                (track_id_for_uri(uri), source, score, time.time(), norm_path(path)),
            )
            return cur.rowcount > 0

    def downloaded_uri(self, path: Path | str) -> str | None:
        """The track this file was linked to as a download, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT t.uri FROM track_files f JOIN tracks t ON t.id = f.track_id WHERE f.path = ? AND f.source = 'download'",
                (norm_path(path),),
            ).fetchone()
        return row[0] if row else None

    def playlists_containing(self, uri: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT playlist_id FROM playlist_tracks WHERE track_id = ?", (track_id_for_uri(uri),)
            ).fetchall()
        return [r[0] for r in rows]

    def local_tracks(self, playlist_id: str) -> tuple[str, list[M3UEntry]]:
        """(playlist name, one entry per track that has a local file, in playlist order)."""
        with self._lock:
            name_row = self._conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            rows = self._conn.execute(
                """
                SELECT (SELECT f.path FROM track_files f WHERE f.track_id = pt.track_id
                        ORDER BY f.source = 'download' DESC, f.linked_at DESC LIMIT 1),
                       t.title, t.artists
                FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
                WHERE pt.playlist_id = ? ORDER BY pt.position
                """,
                (playlist_id,),
            ).fetchall()
        name = name_row[0] if name_row else playlist_id
        seen: set[str] = set()
        entries = []
        for path, title, artists in rows:
            if path and path not in seen:
                seen.add(path)
                entries.append(M3UEntry(file_path=Path(path), title=title, artists=artists))
        return name, entries

    # -- queue history --
    def record_queued(self, tracks, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._upsert_tracks(tracks)
            self._conn.executemany(
                "INSERT OR IGNORE INTO playlists (id, name) VALUES (?, ?)",
                [(t.playlist_id, t.playlist_id) for t in tracks if t.playlist_id],
            )
            self._conn.executemany(
                "INSERT INTO queue_events (track_id, playlist_id, queued_at) VALUES (?, ?, ?)",
                [(track_id_for_uri(t.uri), t.playlist_id or None, now) for t in tracks],
            )

    def match_download(self, path: Path, keys: list[tuple[str, str]], threshold: int) -> str | None:
        """Fuzzy-match a new download, by its (title, artists) keys, against
        recently queued tracks with no local file yet. On a match, links the
        file to that track and returns the track's uri."""
        with self._lock:
            pending = self._conn.execute(
                """
                SELECT DISTINCT t.uri, t.title, t.artists FROM queue_events e JOIN tracks t ON t.id = e.track_id
                WHERE e.queued_at > ? AND NOT EXISTS (SELECT 1 FROM track_files f WHERE f.track_id = t.id)
                """,
                (time.time() - DOWNLOAD_MATCH_WINDOW_SECONDS,),
            ).fetchall()
        index = MatchIndex(pending, keys=lambda row: [(row[1], row[2])])
        for title, artists in keys:
            if not title:
                continue
            row, _ = index.best(title, artists, threshold=threshold)
            if row is not None and self.link(row[0], path, "download"):
                return row[0]
        return None


def rewrite_playlist_files(catalog: Catalog, playlist_ids, m3u_output_dir: Path | str) -> None:
    """(Re)write `<playlist name>.m3u8` for each playlist: every track in it
    that has a local file, in Spotify's order. Playlists with none are skipped."""
    for playlist_id in dict.fromkeys(playlist_ids):
        name, entries = catalog.local_tracks(playlist_id)
        if entries:
            write_playlist_m3u(name, entries, m3u_output_dir)
