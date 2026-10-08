"""spotipy wrapper implementing the API contract in Build Spec §5.

Verify exact response shapes against Spotify's current Web API reference at
build time (§5) -- this wraps the documented API as of the spec's writing.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from .rate_limiter import RateLimiter

logger = logging.getLogger("spotify_queuer.spotify_client")

SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing"
)
# Spotify's current redirect URI validation requires https, or the literal
# loopback IP (not "localhost") for http during development.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
MAX_429_RETRIES = 5
DEFAULT_RETRY_AFTER_FALLBACK = 5.0


class SpotifyClient:
    """All network calls go through _call(), which applies the self-throttle
    rate limiter (§5 rate_limit config) and handles 429/Retry-After exactly
    (§5, fixed behavior, not configurable)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        cache_path: Path | str,
        rate_limiter: RateLimiter,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        open_browser: bool = True,
    ):
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            cache_handler=CacheFileHandler(cache_path=str(cache_path)),
            open_browser=open_browser,
        )
        # retries=0: all retry behavior for this app is handled explicitly in
        # _call(), so 429 backoff timing is exactly what the spec requires.
        self._sp = spotipy.Spotify(auth_manager=auth_manager, retries=0)
        self._rate_limiter = rate_limiter

    def _call(self, fn, *args, **kwargs):
        attempts = 0
        while True:
            self._rate_limiter.acquire()
            try:
                return fn(*args, **kwargs)
            except SpotifyException as exc:
                if exc.http_status == 429:
                    attempts += 1
                    retry_after = _extract_retry_after(exc)
                    if attempts > MAX_429_RETRIES:
                        logger.error("Exceeded max 429 retries (%s)", MAX_429_RETRIES)
                        raise
                    logger.warning("429 rate limited; honoring Retry-After=%ss", retry_after)
                    time.sleep(retry_after)
                    continue
                raise

    # -- playlists --
    def get_playlist(self, playlist_id: str) -> dict:
        return self._call(self._sp.playlist, playlist_id, fields="id,name,snapshot_id,images")

    def list_user_playlists(self) -> list[dict]:
        """Not itself in the §5 endpoint table, but required to populate the playlist
        picker (§12) so the user has something to toggle 'monitored' on."""
        items: list[dict] = []
        results = self._call(self._sp.current_user_playlists, limit=50)
        items.extend(results["items"])
        while results.get("next"):
            results = self._call(self._sp.next, results)
            items.extend(results["items"])
        return items

    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Paginate fully (100/page) before diffing -- a partial page reads as a mass
        removal to the prune feature (§5)."""
        items: list[dict] = []
        results = self._call(self._sp.playlist_items, playlist_id, limit=100, additional_types=("track",))
        items.extend(results["items"])
        while results.get("next"):
            results = self._call(self._sp.next, results)
            items.extend(results["items"])
        # The installed spotipy's playlist_items() hits /playlists/{id}/items,
        # whose response nests each track/episode under "item", not "track"
        # (the older, still-documented key). Accept either.
        tracks = []
        for item in items:
            track = item.get("track") or item.get("item")
            if track:
                tracks.append(track)
        return tracks

    # -- playback --
    def get_playback_state(self) -> dict | None:
        """None corresponds to the 204 'no active device' response (§5, §6)."""
        return self._call(self._sp.current_playback)

    def get_devices(self) -> list[dict]:
        return self._call(self._sp.devices)["devices"]

    def transfer_playback(self, device_id: str, play: bool = True) -> None:
        self._call(self._sp.transfer_playback, device_id=device_id, force_play=play)

    def queue_track(self, uri: str) -> None:
        self._call(self._sp.add_to_queue, uri)

    def start_playback(self, device_id: str, uris: list[str]) -> None:
        """Replace current playback with `uris` in a single request -- the only
        way to queue many tracks at once; unlike queue_track, this interrupts
        whatever's currently playing."""
        self._call(self._sp.start_playback, device_id=device_id, uris=uris)

    def get_queue(self) -> dict:
        """Used to confirm a queue call landed and avoid duplicate adds (§5)."""
        return self._call(self._sp.queue)


def _extract_retry_after(exc: SpotifyException) -> float:
    headers = getattr(exc, "headers", None) or {}
    value = headers.get("Retry-After")
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_FALLBACK
