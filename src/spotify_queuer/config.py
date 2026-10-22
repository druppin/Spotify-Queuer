"""Config schema and load/save."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


def _from_known_fields(cls: type[T], data: dict[str, Any]) -> T:
    """Build a dataclass from a dict, ignoring keys that aren't fields of it --
    keeps hand-edited or older config files loadable across schema changes."""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclasses.dataclass
class SpotifyConfig:
    client_id: str = ""
    client_secret: str = ""
    device_name: str = ""


@dataclasses.dataclass
class QueueConfig:
    """State/preferences for the playlist queue picker."""

    last_selected_playlist_ids: list[str] = dataclasses.field(default_factory=list)
    skip_local_tracks: bool = True
    # True (default): always replace playback with the selected batch in a
    # single start_playback request, interrupting whatever's currently on.
    # False: append behind what's playing instead -- costs one add-to-queue
    # request per track, since Spotify has no bulk queue endpoint.
    interrupt_playback: bool = True


@dataclasses.dataclass
class RateLimitConfig:
    max_requests: int = 90
    window_seconds: int = 30


@dataclasses.dataclass
class DownloadWatchConfig:
    """The folder new downloads land in, watched for files to file into a
    dated 'recently downloaded' .m3u8 for sorting into Mixxx crates."""

    watch_dir: str = ""
    enabled: bool = True
    # Filesystem-event watching can miss things (USB remounts, network
    # drives, events dropped under load) -- this periodic full rescan is the
    # backstop that keeps a missed event from becoming silent data loss.
    fallback_rescan_seconds: int = 60


@dataclasses.dataclass
class PathsConfig:
    music_root: str = ""
    m3u_output_dir: str = ""
    log_dir: str = ""


@dataclasses.dataclass
class MatchingConfig:
    fuzzy_threshold: int = 87


@dataclasses.dataclass
class AppConfig:
    spotify: SpotifyConfig = dataclasses.field(default_factory=SpotifyConfig)
    queue: QueueConfig = dataclasses.field(default_factory=QueueConfig)
    rate_limit: RateLimitConfig = dataclasses.field(default_factory=RateLimitConfig)
    download_watch: DownloadWatchConfig = dataclasses.field(default_factory=DownloadWatchConfig)
    paths: PathsConfig = dataclasses.field(default_factory=PathsConfig)
    matching: MatchingConfig = dataclasses.field(default_factory=MatchingConfig)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def load_or_default(cls, path: Path) -> "AppConfig":
        path = Path(path)
        if path.exists():
            return cls.load(path)
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        return cls(
            spotify=_from_known_fields(SpotifyConfig, data.get("spotify", {})),
            queue=_from_known_fields(QueueConfig, data.get("queue", {})),
            rate_limit=_from_known_fields(RateLimitConfig, data.get("rate_limit", {})),
            download_watch=_from_known_fields(DownloadWatchConfig, data.get("download_watch", {})),
            paths=_from_known_fields(PathsConfig, data.get("paths", {})),
            matching=_from_known_fields(MatchingConfig, data.get("matching", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "spotify": dataclasses.asdict(self.spotify),
            "queue": dataclasses.asdict(self.queue),
            "rate_limit": dataclasses.asdict(self.rate_limit),
            "download_watch": dataclasses.asdict(self.download_watch),
            "paths": dataclasses.asdict(self.paths),
            "matching": dataclasses.asdict(self.matching),
        }

    def save(self, path: Path) -> None:
        """Atomic write: temp file + rename, so a concurrent reader never sees a partial file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
