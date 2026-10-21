#!/usr/bin/env python
"""Standalone tool: find duplicate tracks in the music library by their
title/artist tags (not their filenames), keep the OLDEST copy of each, move
the newer copies out of the library into a quarantine folder, and rename
kept files whose filename is a truncated copy of their title tag (e.g. an old
download named "Song.mp3" whose tag says "Song - Extended Mix").

Two files count as duplicates when their title tag and set of artists match
after normalization and their durations are within --max-duration-diff
seconds (so an extended mix and a radio edit with the same tags stay apart).
Untagged files are never touched.

Nothing is deleted: duplicates are moved to --quarantine (outside the music
root, mirroring their old relative paths) so they can be checked and then
deleted by hand. Playlists in the m3u folder and the download watcher's
seen-files table are rewritten to point at the kept/renamed paths, and every
move/rename is written to a CSV log in the quarantine folder.

Close Spotify Queuer before running with --apply -- its download watcher
would otherwise file the renamed tracks as new downloads.

Usage:
    python tools/library_cleanup.py                 # dry run: report only
    python tools/library_cleanup.py --apply
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mutagen import File as MutagenFile  # noqa: E402
from mutagen import MutagenError  # noqa: E402

from spotify_queuer.config import AppConfig  # noqa: E402
from spotify_queuer.library_index import AUDIO_EXTENSIONS  # noqa: E402
from spotify_queuer.matching import normalize_text, split_artists  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYLIST_EXTENSIONS = {".m3u", ".m3u8"}
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class AudioFile:
    path: Path
    mtime: float
    title: str
    artists: list[str]
    duration: float

    @property
    def dupe_key(self) -> tuple[str, frozenset[str]]:
        return normalize_text(self.title), frozenset(split_artists(self.artists))


def read_audio_file(path: Path) -> AudioFile | None:
    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError) as exc:
        print(f"  ! unreadable, skipped: {path} ({exc})")
        return None
    if audio is None or not audio.tags:
        return None
    title = str((audio.tags.get("title") or [""])[0]).strip()
    artists = [str(a).strip() for a in audio.tags.get("artist") or [] if str(a).strip()]
    if not title or not artists:
        return None
    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    return AudioFile(path, path.stat().st_mtime, title, artists, duration)


def scan(music_root: Path) -> tuple[list[AudioFile], int]:
    files, untagged = [], 0
    for dirpath, _dirnames, filenames in os.walk(music_root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            audio = read_audio_file(Path(dirpath) / name)
            if audio is None:
                untagged += 1
            else:
                files.append(audio)
    return files, untagged


def find_duplicate_groups(files: list[AudioFile], max_duration_diff: float) -> list[list[AudioFile]]:
    """Groups of 2+ copies of the same track, oldest first."""
    by_key: dict[tuple[str, frozenset[str]], list[AudioFile]] = {}
    for f in files:
        by_key.setdefault(f.dupe_key, []).append(f)
    groups = []
    for same_tags in by_key.values():
        if len(same_tags) < 2:
            continue
        # Cluster by duration so different edits sharing tags aren't merged.
        clusters: list[list[AudioFile]] = []
        for f in sorted(same_tags, key=lambda f: f.duration):
            if clusters and f.duration - clusters[-1][-1].duration <= max_duration_diff:
                clusters[-1].append(f)
            else:
                clusters.append([f])
        groups.extend(sorted(c, key=lambda f: f.mtime) for c in clusters if len(c) > 1)
    return sorted(groups, key=lambda g: str(g[0].path).lower())


def title_filename(title: str, suffix: str) -> str:
    # Dropped rather than replaced, as the downloader does ("dashstar*" -> "dashstar - VIP.mp3").
    name = _INVALID_FILENAME_CHARS.sub("", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name + suffix


def is_truncated_title(stem: str, title: str) -> bool:
    """The filename is the title tag with its tail cut off -- the mix/remix
    suffix older downloads lost ("Song.mp3" for "Song - Extended Mix").
    Other mismatches (track-number prefixes, "Artist - Album - 01 Song", bad
    tags like "Track 4") are left alone."""
    stem_n, title_n = normalize_text(stem), normalize_text(title)
    return bool(stem_n) and title_n != stem_n and title_n.startswith(stem_n + " ")


def find_renames(files: list[AudioFile], claimed: set[str]) -> list[tuple[Path, Path]]:
    """(old, new) for files whose filename is a truncated copy of their title tag."""
    renames = []
    for f in files:
        if not is_truncated_title(f.path.stem, f.title):
            continue
        target = f.path.with_name(title_filename(f.title, f.path.suffix))
        key = os.path.normcase(str(target))
        if target.exists() or key in claimed:
            print(f"  ! rename target already exists, skipped: {f.path} -> {target.name}")
            continue
        claimed.add(key)
        renames.append((f.path, target))
    return renames


def rewrite_playlists(playlist_dir: Path, moved: dict[str, Path], apply: bool) -> int:
    """Point playlist entries at new paths; returns how many entries changed."""
    changed_total = 0
    if not playlist_dir.is_dir():
        return 0
    for playlist in sorted(playlist_dir.iterdir()):
        if playlist.suffix.lower() not in PLAYLIST_EXTENSIONS:
            continue
        raw = playlist.read_bytes()
        text = raw.decode("utf-8-sig")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        changed = 0
        for i, line in enumerate(lines):
            if not line.strip() or line.startswith("#"):
                continue
            entry = Path(line) if Path(line).is_absolute() else playlist_dir / line
            target = moved.get(os.path.normcase(os.path.normpath(str(entry))))
            if target is None:
                continue
            lines[i] = str(target) if Path(line).is_absolute() else os.path.relpath(target, playlist_dir)
            changed += 1
        if changed:
            print(f"  playlist {playlist.name}: {changed} entr{'y' if changed == 1 else 'ies'} updated")
            if apply:
                bom = "﻿" if raw.startswith(b"\xef\xbb\xbf") else ""
                tmp = playlist.with_name(playlist.name + ".tmp")
                tmp.write_text(bom + newline.join(lines) + newline, encoding="utf-8", newline="")
                tmp.replace(playlist)
        changed_total += changed
    return changed_total


def update_seen_files(state_db: Path, renames: list[tuple[Path, Path]], apply: bool) -> int:
    """Re-key renamed files in the download watcher's seen-files table so they aren't filed as new downloads."""
    if not state_db.exists() or not renames:
        return 0
    conn = sqlite3.connect(state_db)
    try:
        updated = 0
        for old, new in renames:
            row = conn.execute("SELECT 1 FROM seen_files WHERE path = ?", (str(old.resolve()),)).fetchone()
            if row is None:
                continue
            updated += 1
            if apply:
                stat = new.stat()
                conn.execute(
                    "UPDATE seen_files SET path = ?, size = ?, mtime = ? WHERE path = ?",
                    (str(new.resolve()), stat.st_size, stat.st_mtime, str(old.resolve())),
                )
        if apply:
            conn.commit()
        return updated
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.json"), help="Spotify Queuer config.json")
    parser.add_argument("--quarantine", help="Where duplicates are moved (default: <music root drive>/_Duplicates)")
    parser.add_argument("--max-duration-diff", type=float, default=2.0, help="Seconds; default 2")
    parser.add_argument("--state-db", default=str(REPO_ROOT / "download_watcher_state.sqlite3"))
    parser.add_argument("--apply", action="store_true", help="Actually move/rename files (default is a dry run)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # track names aren't limited to the console's code page

    config = AppConfig.load(Path(args.config))
    music_root = Path(config.paths.music_root).resolve()
    playlist_dir = Path(config.paths.m3u_output_dir).resolve()
    quarantine = Path(args.quarantine) if args.quarantine else Path(music_root.anchor) / "_Duplicates"
    if quarantine.resolve().is_relative_to(music_root):
        print(f"Quarantine folder must be outside the music root: {quarantine}", file=sys.stderr)
        return 1

    print(f"{'APPLYING' if args.apply else 'DRY RUN'} -- scanning {music_root} ...")
    files, untagged = scan(music_root)
    print(f"{len(files)} tagged file(s), {untagged} untagged/unreadable (left alone)\n")

    groups = find_duplicate_groups(files, args.max_duration_diff)
    removals = [(dupe, group[0]) for group in groups for dupe in group[1:]]
    print(f"== {len(groups)} duplicate group(s), {len(removals)} newer cop{'y' if len(removals) == 1 else 'ies'} to quarantine")
    for group in groups:
        keep = group[0]
        print(f"  KEEP  {keep.path}  ({datetime.fromtimestamp(keep.mtime):%Y-%m-%d})")
        for dupe in group[1:]:
            print(f"  DUPE  {dupe.path}  ({datetime.fromtimestamp(dupe.mtime):%Y-%m-%d})")

    removed = {os.path.normcase(str(d.path)) for d, _ in removals}
    kept_files = [f for f in files if os.path.normcase(str(f.path)) not in removed]
    renames = find_renames(kept_files, claimed=set())
    print(f"\n== {len(renames)} filename(s) truncated from their title tag")
    for old, new in renames:
        print(f"  {old}\n    -> {new.name}")

    # Old path -> where playlists should now point.
    final_path = {os.path.normcase(str(old)): new for old, new in renames}
    moved = {}
    for dupe, keep in removals:
        moved[os.path.normcase(str(dupe.path))] = final_path.get(os.path.normcase(str(keep.path)), keep.path)
    moved.update(final_path)

    if not args.apply:
        print("\n== Playlists")
        rewrite_playlists(playlist_dir, moved, apply=False)
        seen = update_seen_files(Path(args.state_db), renames, apply=False)
        print(f"\n== {seen} download-watcher record(s) would be re-keyed")
        print("\nDry run only. Re-run with --apply to make these changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    quarantine.mkdir(parents=True, exist_ok=True)
    log_path = quarantine / f"cleanup-{stamp}.csv"
    with log_path.open("w", newline="", encoding="utf-8") as log_file:
        log = csv.writer(log_file)
        log.writerow(["action", "from", "to", "kept_copy"])
        for dupe, keep in removals:
            dest = quarantine / dupe.path.relative_to(music_root)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem} ({stamp}){dest.suffix}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dupe.path, dest)
            log.writerow(["quarantined", dupe.path, dest, keep.path])
            _remove_empty_dirs(dupe.path.parent, music_root)
        for old, new in renames:
            old.rename(new)
            log.writerow(["renamed", old, new, ""])
    rewrite_playlists(playlist_dir, moved, apply=True)
    seen = update_seen_files(Path(args.state_db), renames, apply=True)
    print(f"\n{len(removals)} quarantined to {quarantine}, {len(renames)} renamed, {seen} watcher record(s) re-keyed.")
    print(f"Log: {log_path}")
    return 0


def _remove_empty_dirs(folder: Path, music_root: Path) -> None:
    """Remove folders left completely empty by a move, up to the music root."""
    while folder != music_root and folder.is_relative_to(music_root):
        if any(folder.iterdir()):
            return
        folder.rmdir()
        folder = folder.parent


if __name__ == "__main__":
    raise SystemExit(main())
