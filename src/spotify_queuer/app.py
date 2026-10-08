"""Wires config, logging, the Spotify client, library index and download
watcher together. GUI-agnostic -- gui/main_window.py builds on top of this."""
from __future__ import annotations

from pathlib import Path

from . import logging_setup
from .config import AppConfig
from .download_watcher import DownloadWatcher
from .library_index import LibraryIndex
from .queue_engine import QueueResult, run_queue
from .rate_limiter import RateLimiter
from .spotify_client import SpotifyClient

DOWNLOAD_STATE_DB_FILENAME = "download_watcher_state.sqlite3"
TOKEN_CACHE_FILENAME = ".spotify_token_cache"


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
        self.library_index = LibraryIndex(self.config.paths.music_root)
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
            state_db_path=self.config_path.parent / DOWNLOAD_STATE_DB_FILENAME,
            fallback_rescan_seconds=self.config.download_watch.fallback_rescan_seconds,
            on_new_file=on_new_file,
            on_error=on_error,
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

    def save_config(self) -> None:
        self.config.save(self.config_path)

    # -- queue picker --
    def queue_selected_playlists(
        self, playlist_ids: list[str], interrupt_playback: bool | None = None, on_progress=None
    ) -> QueueResult:
        """interrupt_playback=None uses the configured default (the main
        "Queue Selected" button); pass False explicitly for "Add After
        Current", which must always append regardless of that setting."""
        interrupt = self.config.queue.interrupt_playback if interrupt_playback is None else interrupt_playback
        result = run_queue(
            self.spotify,
            self.library_index,
            self.config.spotify.device_name,
            playlist_ids,
            skip_local=self.config.queue.skip_local_tracks,
            interrupt_playback=interrupt,
            fuzzy_threshold=self.config.matching.fuzzy_threshold,
            on_progress=on_progress,
        )
        self.config.queue.last_selected_playlist_ids = list(playlist_ids)
        self.save_config()
        return result
