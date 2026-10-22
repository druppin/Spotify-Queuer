"""Wires config, logging, the Spotify client, library index and download
watcher together. GUI-agnostic -- gui/main_window.py builds on top of this."""
from __future__ import annotations

from pathlib import Path

import logging
import sqlite3

from . import logging_setup
from .catalog import Catalog, rewrite_playlist_files
from .config import AppConfig
from .db import ensure_migrated
from .download_watcher import DownloadWatcher
from .library_index import LibraryIndex
from .mixxx_status import default_mixxx_db_path, needs_prep, sync_prep_status, write_needs_prep_playlist
from .queue_engine import QueueResult, run_queue
from .rate_limiter import RateLimiter
from .spotify_client import SpotifyClient

DB_FILENAME = "spotify_queuer.sqlite3"
TOKEN_CACHE_FILENAME = ".spotify_token_cache"

logger = logging.getLogger("spotify_queuer.app")


def default_config_path() -> Path:
    return Path.cwd() / "config.json"


class Application:
    def __init__(self, config_path: Path | str, open_browser: bool = True):
        self.config_path = Path(config_path)
        self.config = AppConfig.load_or_default(self.config_path)

        logging_setup.setup_logging(self.config.paths.log_dir or (self.config_path.parent / "logs"))

        self.rate_limiter = RateLimiter(
            max_requests=self.config.rate_limit.max_requests,
            window_seconds=self.config.rate_limit.window_seconds,
        )
        self.spotify = SpotifyClient(
            client_id=self.config.spotify.client_id,
            client_secret=self.config.spotify.client_secret,
            cache_path=self.config_path.parent / TOKEN_CACHE_FILENAME,
            rate_limiter=self.rate_limiter,
            open_browser=open_browser,
        )
        self.db_path = self.config_path.parent / DB_FILENAME
        ensure_migrated(self.db_path, legacy_dir=self.config_path.parent)
        self.library_index = LibraryIndex(self.config.paths.music_root, db_path=self.db_path)
        self.catalog = Catalog(self.db_path)
        self._warned_missing_mixxx_db = False
        self.download_watcher: DownloadWatcher | None = None

    # -- download watcher lifecycle (GUI wires its bridge callbacks in before starting) --
    def start_download_watcher(self, on_new_file=None, on_error=None) -> None:
        if self.download_watcher is not None or not self.config.download_watch.enabled:
            return
        if not self.config.download_watch.watch_dir:
            return
        self.download_watcher = DownloadWatcher(
            watch_dir=self.config.download_watch.watch_dir,
            m3u_output_dir=self.config.paths.m3u_output_dir,
            state_db_path=self.db_path,
            fallback_rescan_seconds=self.config.download_watch.fallback_rescan_seconds,
            on_new_file=on_new_file,
            on_error=on_error,
            catalog=self.catalog,
            fuzzy_threshold=self.config.matching.fuzzy_threshold,
            after_rescan=self.refresh_prep_status,
        )
        self.download_watcher.start()

    def restart_download_watcher(self, on_new_file=None, on_error=None) -> None:
        """Call after settings that affect the watcher (watch_dir, etc.) change."""
        self.stop()
        self.start_download_watcher(on_new_file, on_error)

    def stop(self) -> None:
        if self.download_watcher is not None:
            self.download_watcher.stop()
            self.download_watcher = None

    # -- Mixxx prep tracking --
    def refresh_prep_status(self) -> None:
        """Re-read Mixxx's prep status for new downloads and rewrite `Needs prep.m3u8`.
        Called on start and after each downloads rescan, off the GUI thread."""
        mixxx_db = Path(self.config.paths.mixxx_db) if self.config.paths.mixxx_db else default_mixxx_db_path()
        if not mixxx_db.exists():
            if not self._warned_missing_mixxx_db:
                logger.warning("Mixxx database not found at %s; prep tracking is off (set it in Settings)", mixxx_db)
                self._warned_missing_mixxx_db = True
            return
        try:
            sync_prep_status(self.db_path, mixxx_db, music_root=self.config.paths.music_root or None)
        except sqlite3.Error:
            logger.warning("Could not read the Mixxx database; will retry on the next rescan", exc_info=True)
            return
        if self.config.paths.m3u_output_dir:
            try:
                write_needs_prep_playlist(self.db_path, self.config.paths.m3u_output_dir)
            except OSError:
                logger.exception("Failed to write the Needs prep playlist")

    def needs_prep_count(self) -> int:
        return len(needs_prep(self.db_path))

    def save_config(self) -> None:
        self.config.save(self.config_path)

    # -- queue picker --
    def queue_selected_playlists(
        self,
        playlist_ids: list[str],
        interrupt_playback: bool | None = None,
        on_progress=None,
    ) -> QueueResult:
        """interrupt_playback=None uses the configured default (the main
        "Queue Selected" button); pass False explicitly for "Add After
        Current", which must always append regardless of that setting."""
        interrupt = self.config.queue.interrupt_playback if interrupt_playback is None else interrupt_playback
        # Listed fresh (one request per 50 playlists) for current snapshot_ids,
        # so an unchanged playlist's tracks can come from the database instead.
        if on_progress is not None:
            on_progress("Checking your playlists for changes...")
        playlists = self.spotify.list_user_playlists()
        self.catalog.sync_playlists(playlists)
        result = run_queue(
            self.spotify,
            self.library_index,
            self.config.spotify.device_name,
            playlist_ids,
            skip_local=self.config.queue.skip_local_tracks,
            interrupt_playback=interrupt,
            fuzzy_threshold=self.config.matching.fuzzy_threshold,
            on_progress=on_progress,
            catalog=self.catalog,
            snapshots={p["id"]: p.get("snapshot_id") for p in playlists},
        )
        if self.config.paths.m3u_output_dir:
            try:
                rewrite_playlist_files(self.catalog, playlist_ids, self.config.paths.m3u_output_dir)
            except OSError:
                logger.exception("Failed to write source playlist files")
        self.config.queue.last_selected_playlist_ids = list(playlist_ids)
        self.save_config()
        return result
