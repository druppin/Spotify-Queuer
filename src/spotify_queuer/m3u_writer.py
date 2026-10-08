"""Extended M3U8 playlist output, per Build Spec §9.

One file per tracked playlist, named after the playlist (sanitized), UTF-8,
track order mirrors Spotify playlist order, paths relative to m3u_output_dir.
Writes are atomic (temp file + rename) so an app with the file open never
reads a half-written file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip()
    name = name.rstrip(". ")
    return name or "playlist"


@dataclass
class M3UEntry:
    file_path: Path
    title: str
    artists: str
    duration_seconds: int = -1


def playlist_m3u_path(playlist_name: str, m3u_output_dir: Path | str) -> Path:
    return Path(m3u_output_dir) / f"{sanitize_filename(playlist_name)}.m3u8"


def write_playlist_m3u(playlist_name: str, entries: list[M3UEntry], m3u_output_dir: Path | str) -> Path:
    """Fully (re)write a playlist's .m3u8 atomically.

    Entries must already be in the desired (Spotify playlist mirroring) order.
    Rewriting the whole file each time keeps ordering correct and makes prune
    removals trivial: the pruned track is simply omitted from the next write.
    """
    m3u_output_dir = Path(m3u_output_dir)
    m3u_output_dir.mkdir(parents=True, exist_ok=True)
    out_path = playlist_m3u_path(playlist_name, m3u_output_dir)

    lines = ["#EXTM3U"]
    for entry in entries:
        rel_path = os.path.relpath(entry.file_path, m3u_output_dir)
        artist_title = f"{entry.artists} - {entry.title}" if entry.artists else entry.title
        lines.append(f"#EXTINF:{entry.duration_seconds},{artist_title}")
        lines.append(rel_path)
    content = "\n".join(lines) + "\n"

    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8", newline="\n")
    tmp_path.replace(out_path)
    return out_path
