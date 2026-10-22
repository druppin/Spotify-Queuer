"""Tracks whether new downloads are set-ready in Mixxx: at least one hot cue,
a star rating, and a locked BPM (you lock it once the beatgrid is checked).

Mixxx's own database is only ever read (opened read-only, so this is safe
while Mixxx is running). Only files flagged needs_prep -- downloads the
watcher saw arrive -- are tracked; older library files are never listed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from .db import connect
from .m3u_writer import M3UEntry, write_playlist_m3u

logger = logging.getLogger("spotify_queuer.mixxx_status")

NEEDS_PREP_PLAYLIST = "Needs prep"


def default_mixxx_db_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Mixxx" / "mixxxdb.sqlite"


def _key(path: str) -> str:
    # Mixxx stores "G:/Music/..."; the scan stores "G:\Music\..." -- compare normalized, case-folded on Windows.
    return os.path.normcase(os.path.normpath(path))


def sync_prep_status(db_path: Path | str, mixxx_db_path: Path | str) -> None:
    """Refresh mixxx_status for every needs_prep file from Mixxx's database."""
    conn = connect(db_path)
    try:
        wanted = {_key(r[0]): r[0] for r in conn.execute("SELECT path FROM library_files WHERE needs_prep = 1")}
        if not wanted:
            return
        mixxx = sqlite3.connect(f"file:{Path(mixxx_db_path).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = mixxx.execute(
                """
                SELECT l.id, tl.location, COALESCE(l.rating, 0), COALESCE(l.bpm_lock, 0),
                       (SELECT COUNT(*) FROM cues c WHERE c.track_id = l.id AND c.hotcue >= 0)
                FROM library l JOIN track_locations tl ON l.location = tl.id
                WHERE l.mixxx_deleted IS NOT 1
                """
            ).fetchall()
        finally:
            mixxx.close()
        now = time.time()
        with conn:
            conn.execute("DELETE FROM mixxx_status WHERE path IN (SELECT path FROM library_files WHERE needs_prep = 1)")
            conn.executemany(
                "INSERT OR REPLACE INTO mixxx_status (path, mixxx_id, hotcue_count, rating, bpm_locked, synced_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (wanted[_key(location)], mixxx_id, hotcues, rating, bpm_lock, now)
                    for mixxx_id, location, rating, bpm_lock, hotcues in rows
                    if location and _key(location) in wanted
                ],
            )
    finally:
        conn.close()


def needs_prep(db_path: Path | str) -> list[tuple[Path, list[str]]]:
    """(file, what's missing) for each new download that isn't set-ready yet
    and still exists, oldest first."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT f.path, s.path IS NOT NULL, COALESCE(s.hotcue_count, 0), COALESCE(s.rating, 0),
                   COALESCE(s.bpm_locked, 0)
            FROM library_files f LEFT JOIN mixxx_status s ON s.path = f.path
            WHERE f.needs_prep = 1 ORDER BY f.first_seen_at, f.path
            """
        ).fetchall()
    finally:
        conn.close()
    result = []
    for path, in_mixxx, hotcues, rating, bpm_locked in rows:
        if not in_mixxx:
            missing = ["not in Mixxx yet"]
        else:
            missing = [label for ok, label in ((hotcues, "no hot cues"), (rating, "unrated"), (bpm_locked, "BPM not locked")) if not ok]
        if missing and Path(path).exists():
            result.append((Path(path), missing))
    return result


def write_needs_prep_playlist(db_path: Path | str, m3u_output_dir: Path | str) -> int:
    """(Re)write `Needs prep.m3u8`; returns how many tracks it lists."""
    pending = needs_prep(db_path)
    entries = []
    for path, missing in pending:
        title, _, artists = path.stem.partition(" - ")
        entries.append(M3UEntry(file_path=path, title=f"{title.strip()} [{', '.join(missing)}]", artists=artists.strip()))
    write_playlist_m3u(NEEDS_PREP_PLAYLIST, entries, m3u_output_dir)
    return len(entries)
