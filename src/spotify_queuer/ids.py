# Copied unchanged from druppin/cratesync common/ids.py @ e183088; keep the two identical.
"""Read and write the stable track ID (SPOTIFY_ID tag), per findings spike 5.

Supported containers are the ones in the library: MP3 and WAV (ID3 TXXX frame)
and M4A (iTunes freeform atom).
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

import mutagen
from mutagen.id3 import ID3, TXXX
from mutagen.mp4 import MP4Tags

KEY = "SPOTIFY_ID"
MP4_KEY = f"----:com.apple.iTunes:{KEY}"
SUPPORTED = {".mp3", ".m4a", ".wav"}
_ID_RE = re.compile(r"[0-9A-Za-z]{22}")  # Spotify track IDs are 22 base62 characters


def _open(path: Path):
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError(f"unsupported format: {path.name}")
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError(f"not readable as audio: {path.name}")
    return audio


def read_id(path) -> str | None:
    tags = _open(Path(path)).tags
    if isinstance(tags, ID3):
        frame = tags.get(f"TXXX:{KEY}")
        return frame.text[0] if frame else None
    if isinstance(tags, MP4Tags):
        values = tags.get(MP4_KEY)
        return bytes(values[0]).decode() if values else None
    return None


def write_id(path, spotify_id: str) -> None:
    """Tag a temp copy, then rename it over the original, so a crash never leaves
    a half-written file (and Syncthing never sees one)."""
    if not _ID_RE.fullmatch(spotify_id):
        raise ValueError(f"not a Spotify track ID: {spotify_id!r}")
    path = Path(path)
    _open(path)  # validate before copying
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cratesync-", suffix=path.suffix)
    os.close(fd)
    try:
        shutil.copy2(path, tmp)
        audio = mutagen.File(tmp)
        if audio.tags is None:
            audio.add_tags()
        if isinstance(audio.tags, ID3):
            audio.tags.delall(f"TXXX:{KEY}")
            audio.tags.add(TXXX(encoding=3, desc=KEY, text=[spotify_id]))
        else:
            audio.tags[MP4_KEY] = [spotify_id.encode()]
        audio.save()
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
