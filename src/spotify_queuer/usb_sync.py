"""Additive/update-only sync between two directory trees (e.g. two DJ USB
sticks): copies files that are new or changed on the source, and never
deletes anything from the destination, even if it's gone from the source.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("spotify_queuer.usb_sync")

# FAT32 (common on USB sticks) stores mtime with 2-second resolution, so an
# exact-equality comparison flags files as "changed" that are actually
# identical copies -- allow a small tolerance instead.
MTIME_TOLERANCE_SECONDS = 2.0

# Copied in chunks (rather than one shutil.copy2 call) so callers can report
# live progress through a large file instead of going quiet until it finishes.
_COPY_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class SyncAction:
    relative_path: Path
    source_path: Path
    dest_path: Path
    reason: str  # "new" or "changed"
    size: int


def plan_sync(source_root: Path | str, dest_root: Path | str) -> list[SyncAction]:
    """Compare every file under source_root against dest_root by relative
    path, using (size, mtime) as the change signal -- cheap, no file content
    has to be read. Returns what *would* be copied; copies nothing itself."""
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    actions: list[SyncAction] = []
    for src_path in sorted(source_root.rglob("*")):
        if not src_path.is_file():
            continue
        rel = src_path.relative_to(source_root)
        dest_path = dest_root / rel
        src_stat = src_path.stat()
        if not dest_path.exists():
            actions.append(SyncAction(rel, src_path, dest_path, "new", src_stat.st_size))
            continue
        dest_stat = dest_path.stat()
        size_differs = src_stat.st_size != dest_stat.st_size
        mtime_differs = abs(src_stat.st_mtime - dest_stat.st_mtime) > MTIME_TOLERANCE_SECONDS
        if size_differs or mtime_differs:
            actions.append(SyncAction(rel, src_path, dest_path, "changed", src_stat.st_size))
    return actions


def apply_sync(actions: list[SyncAction], progress_cb=None, chunk_cb=None) -> int:
    """Copy every planned action; never deletes anything. Returns count copied.

    progress_cb(action): called once a file has finished copying.
    chunk_cb(action, bytes_copied): called repeatedly while a single file is
    copying (starting at 0) so callers can show live progress on large files
    instead of the UI going quiet until the whole file lands.
    """
    copied = 0
    for action in actions:
        action.dest_path.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(action, chunk_cb)
        copied += 1
        logger.info("Synced %s (%s)", action.relative_path, action.reason)
        if progress_cb is not None:
            progress_cb(action)
    return copied


def _copy_file(action: SyncAction, chunk_cb=None) -> None:
    with open(action.source_path, "rb") as fsrc, open(action.dest_path, "wb") as fdst:
        copied_bytes = 0
        if chunk_cb is not None:
            chunk_cb(action, copied_bytes)
        while True:
            chunk = fsrc.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            fdst.write(chunk)
            copied_bytes += len(chunk)
            if chunk_cb is not None:
                chunk_cb(action, copied_bytes)
    shutil.copystat(action.source_path, action.dest_path)
