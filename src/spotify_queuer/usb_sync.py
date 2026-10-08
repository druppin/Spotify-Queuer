"""Additive/update-only sync between two directory trees (e.g. two DJ USB
sticks): copies files that are new or changed on the source, and never
deletes anything from the destination, even if it's gone from the source.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class TrackDiff:
    """One file that differs between the two trees, with both sides' details
    so the GUI can show *how* it differs -- e.g. a destination copy that's
    much smaller than the source is probably a truncated/corrupt copy, and a
    file that only exists on the destination may need re-downloading."""

    relative_path: Path
    status: str  # "new" (source only), "changed" (on both, differs), "dest_only" (destination only)
    source_size: int | None
    dest_size: int | None
    source_mtime: float | None
    dest_mtime: float | None

    @property
    def size_delta(self) -> int | None:
        """dest_size - source_size; None unless the file is on both sides."""
        if self.source_size is None or self.dest_size is None:
            return None
        return self.dest_size - self.source_size

    @property
    def note(self) -> str:
        if self.status == "new":
            return "Missing on destination"
        if self.status == "dest_only":
            return "Missing on source -- not copied (sync never deletes)"
        if self.source_size == 0:
            return "Source file is empty -- likely needs re-downloading"
        if self.dest_size == 0:
            return "Destination file is empty"
        delta = self.size_delta
        if delta:
            larger = "Destination" if delta > 0 else "Source"
            pct = abs(delta) / max(self.source_size, self.dest_size) * 100
            return f"{larger} is larger by {format_bytes(abs(delta))} ({pct:.0f}%)"
        newer = "Source" if (self.source_mtime or 0) > (self.dest_mtime or 0) else "Destination"
        return f"Same size, {newer.lower()} is newer"


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"


def _file_stats(root: Path) -> dict[Path, os.stat_result]:
    stats = {}
    for path in root.rglob("*"):
        if path.is_file():
            stats[path.relative_to(root)] = path.stat()
    return stats


def compare_trees(source_root: Path | str, dest_root: Path | str) -> list[TrackDiff]:
    """Every file that differs between source_root and dest_root by relative
    path -- new on source, changed (size, or mtime beyond tolerance), or only
    on the destination -- using (size, mtime) so no file content is read.
    Sorted by relative path."""
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    source_stats = _file_stats(source_root)
    dest_stats = _file_stats(dest_root) if dest_root.exists() else {}
    diffs: list[TrackDiff] = []
    for rel in sorted(source_stats.keys() | dest_stats.keys()):
        src, dst = source_stats.get(rel), dest_stats.get(rel)
        if dst is None:
            diffs.append(TrackDiff(rel, "new", src.st_size, None, src.st_mtime, None))
        elif src is None:
            diffs.append(TrackDiff(rel, "dest_only", None, dst.st_size, None, dst.st_mtime))
        elif src.st_size != dst.st_size or abs(src.st_mtime - dst.st_mtime) > MTIME_TOLERANCE_SECONDS:
            diffs.append(TrackDiff(rel, "changed", src.st_size, dst.st_size, src.st_mtime, dst.st_mtime))
    return diffs


def actions_from_diffs(diffs: list[TrackDiff], source_root: Path | str, dest_root: Path | str) -> list[SyncAction]:
    """The copies a sync would make for `diffs`: new and changed files only,
    never anything that exists only on the destination."""
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    return [
        SyncAction(d.relative_path, source_root / d.relative_path, dest_root / d.relative_path, d.status, d.source_size)
        for d in diffs
        if d.status in ("new", "changed")
    ]


def plan_sync(source_root: Path | str, dest_root: Path | str) -> list[SyncAction]:
    """Compare every file under source_root against dest_root by relative
    path, using (size, mtime) as the change signal -- cheap, no file content
    has to be read. Returns what *would* be copied; copies nothing itself."""
    return actions_from_diffs(compare_trees(source_root, dest_root), source_root, dest_root)


def write_diff_csv(diffs: list[TrackDiff], csv_path: Path | str) -> None:
    """Export the diff list, e.g. to work through which tracks to re-download."""
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["status", "path", "source_size", "dest_size", "size_delta", "source_modified", "dest_modified", "note"]
        )
        for d in diffs:
            writer.writerow(
                [
                    d.status,
                    str(d.relative_path),
                    "" if d.source_size is None else d.source_size,
                    "" if d.dest_size is None else d.dest_size,
                    "" if d.size_delta is None else d.size_delta,
                    format_mtime(d.source_mtime),
                    format_mtime(d.dest_mtime),
                    d.note,
                ]
            )


def format_mtime(mtime: float | None) -> str:
    return "" if mtime is None else datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


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
