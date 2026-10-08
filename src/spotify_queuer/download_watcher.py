"""Watches a downloads folder for new audio files and files them into a dated
and timestamped `recently downloaded <date> <time>.m3u8` so they're easy to
find and sort into Mixxx crates later, and so a copy sitting on a USB stick
shows at a glance (by filename) how current it is.

Reliability is the whole point of this module -- a missed file is real data
loss and Mixxx has no safety net of its own (it only shows a "date added"
view of whatever it already knows about; nothing watches for new files on
its behalf). So detection uses BOTH a filesystem-event watcher (near-instant)
AND a periodic full-directory rescan (the backstop for dropped events, USB
remounts, network drives, or the watcher failing to start at all). Both
paths converge on the same seen-files table, so nothing is double-filed, and
a failure of either half is surfaced loudly via on_error rather than logged
and forgotten.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .library_index import AUDIO_EXTENSIONS
from .m3u_writer import M3UEntry, sanitize_filename, write_playlist_m3u

logger = logging.getLogger("spotify_queuer.download_watcher")

DAILY_PLAYLIST_PREFIX = "recently downloaded"
DEFAULT_STABLE_SECONDS = 5.0


def daily_playlist_name(moment: datetime) -> str:
    return f"{DAILY_PLAYLIST_PREFIX} {moment.strftime('%Y-%m-%d %H-%M')}"


def _title_artists_from_filename(path: Path) -> tuple[str, str]:
    """Best-effort label for the m3u entry; matches the `{Title} - {Artists}`
    filename convention used elsewhere in this app (library_index.py)."""
    stem = path.stem
    if " - " in stem:
        title, _, artists = stem.partition(" - ")
        return title.strip(), artists.strip()
    return stem, ""


class SeenFiles:
    """Persisted record of files already filed, keyed by resolved path +
    size + mtime -- a restart doesn't re-log everything, but a file that
    changed (re-downloaded, overwritten with different content) is treated
    as new again. Also the source of truth for "what's new today": the m3u8
    is rebuilt from this table rather than tracked separately, so an app
    restart mid-day doesn't lose earlier entries.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                first_seen_date TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def mark_if_new(self, path: Path, size: int, mtime: float, today: date) -> bool:
        """Record (path, size, mtime); returns True if this is new (never
        seen, or seen before with a different size/mtime)."""
        key = str(path.resolve())
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime FROM seen_files WHERE path = ?", (key,)
            ).fetchone()
            if row is not None and row[0] == size and row[1] == mtime:
                return False
            self._conn.execute(
                """
                INSERT INTO seen_files (path, size, mtime, first_seen_date) VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime
                """,
                (key, size, mtime, today.isoformat()),
            )
            self._conn.commit()
            return True

    def paths_for_date(self, day: date) -> list[Path]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM seen_files WHERE first_seen_date = ? ORDER BY path", (day.isoformat(),)
            ).fetchall()
        return [Path(r[0]) for r in rows]


def rewrite_daily_playlist(seen: SeenFiles, m3u_output_dir: Path | str, today: date, now: datetime | None = None) -> Path:
    """(Re)writes today's playlist under a filename stamped with the current
    time, and removes the file(s) it replaces -- so the single most-current
    file for today always carries an up-to-date timestamp in its name rather
    than leaving stale near-duplicates behind."""
    now = now or datetime.now()
    entries = []
    for path in seen.paths_for_date(today):
        if not path.exists():
            continue  # moved/deleted since being logged -- don't write a dangling entry
        title, artists = _title_artists_from_filename(path)
        entries.append(M3UEntry(file_path=path, title=title, artists=artists))
    new_path = write_playlist_m3u(daily_playlist_name(now), entries, m3u_output_dir)
    _remove_stale_daily_playlists(m3u_output_dir, today, keep=new_path)
    return new_path


def _remove_stale_daily_playlists(m3u_output_dir: Path | str, today: date, keep: Path) -> None:
    m3u_output_dir = Path(m3u_output_dir)
    prefix = sanitize_filename(f"{DAILY_PLAYLIST_PREFIX} {today.isoformat()}")
    for old in m3u_output_dir.glob(f"{prefix}*.m3u8"):
        if old != keep:
            try:
                old.unlink()
            except OSError:
                logger.warning("Failed to remove stale daily playlist: %s", old)


class _Handler(FileSystemEventHandler):
    def __init__(self, check_fn):
        self._check_fn = check_fn

    def on_created(self, event) -> None:
        self._maybe_check(event.src_path, event.is_directory)

    def on_modified(self, event) -> None:
        self._maybe_check(event.src_path, event.is_directory)

    def on_moved(self, event) -> None:
        self._maybe_check(event.dest_path, event.is_directory)

    def _maybe_check(self, raw_path: str, is_directory: bool) -> None:
        if is_directory:
            return
        path = Path(raw_path)
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            self._check_fn(path)


class DownloadWatcher:
    """Owns the watchdog Observer plus the fallback rescan thread.

    on_new_file(path): called (off the GUI thread) whenever a file is filed
    into today's playlist -- wire to a GUI signal for live feedback.
    on_error(message): called when watching itself is degraded (folder
    missing, watcher failed to start, scan error) -- treat as urgent, not a
    routine log line, since a silent failure here is exactly the "big deal"
    outcome this module exists to prevent.
    """

    def __init__(
        self,
        watch_dir: Path | str,
        m3u_output_dir: Path | str,
        state_db_path: Path | str,
        fallback_rescan_seconds: int = 60,
        stable_seconds: float = DEFAULT_STABLE_SECONDS,
        on_new_file=None,
        on_error=None,
    ):
        self.watch_dir = Path(watch_dir)
        self.m3u_output_dir = Path(m3u_output_dir)
        self.fallback_rescan_seconds = fallback_rescan_seconds
        self.stable_seconds = stable_seconds
        self._seen = SeenFiles(state_db_path)
        self._on_new_file = on_new_file or (lambda path: None)
        self._on_error = on_error or (lambda message: None)
        self._observer: Observer | None = None
        self._stop_event = threading.Event()
        self._rescan_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._stop_event.clear()
        self._start_observer()
        self._rescan_thread = threading.Thread(target=self._rescan_loop, daemon=True)
        self._rescan_thread.start()

    def _start_observer(self) -> None:
        if not self.watch_dir.exists():
            self._on_error(f"Downloads folder not found: {self.watch_dir}")
            return
        try:
            observer = Observer()
            observer.schedule(_Handler(self._check_and_file), str(self.watch_dir), recursive=True)
            observer.start()
            self._observer = observer
        except OSError:
            logger.exception("Failed to start filesystem watcher; relying on periodic rescan only")
            self._on_error(
                "Filesystem watcher failed to start; falling back to periodic scanning only "
                f"(every {self.fallback_rescan_seconds}s)."
            )

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._rescan_thread is not None:
            self._rescan_thread.join(timeout=5)
            self._rescan_thread = None
        self._seen.close()

    def _rescan_loop(self) -> None:
        while not self._stop_event.wait(self.fallback_rescan_seconds):
            self.rescan_now()

    def rescan_now(self) -> None:
        if not self.watch_dir.exists():
            self._on_error(f"Downloads folder not found: {self.watch_dir}")
            return
        try:
            candidates = [
                p for p in self.watch_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            ]
        except OSError as exc:
            logger.exception("Error scanning downloads folder")
            self._on_error(f"Error scanning downloads folder: {exc}")
            return
        for path in candidates:
            self._check_and_file(path)

    def _check_and_file(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return  # vanished between listing and stat; a later pass will catch it if it's still there
        if time.time() - stat.st_mtime < self.stable_seconds:
            return  # still being written -- re-checked on the next event or rescan
        now = datetime.now()
        today = now.date()
        with self._lock:
            if not self._seen.mark_if_new(path, stat.st_size, stat.st_mtime, today):
                return
            try:
                write_path = rewrite_daily_playlist(self._seen, self.m3u_output_dir, today, now)
            except OSError as exc:
                logger.exception("Failed to write daily download playlist")
                self._on_error(f"Failed to write '{daily_playlist_name(now)}.m3u8': {exc}")
                return
        logger.info("New download filed: %s -> %s", path, write_path.name)
        self._on_new_file(path)
