#!/usr/bin/env python
"""Standalone tool: copy Mixxx track metadata (ratings, hot cues, play
counts, beatgrid, key, comments, crate memberships) from one mixxxdb.sqlite
to another -- for when you need to bring a second USB stick or Mixxx
instance up to date with the one you've actually been curating.

Run this with Mixxx CLOSED on both sides -- Mixxx holds its database open
while running, and this needs exclusive write access to the destination
file. It always makes a timestamped backup of the destination database
before writing anything, and the whole migration is one all-or-nothing
transaction on top of that.

Usage:
    python tools/mixxx_metadata_migrate.py --source D:\\...\\mixxxdb.sqlite --dest E:\\...\\mixxxdb.sqlite
    python tools/mixxx_metadata_migrate.py --source ... --dest ... --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spotify_queuer.mixxx_migrate import migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Path to the source mixxxdb.sqlite (read-only)")
    parser.add_argument("--dest", required=True, help="Path to the destination mixxxdb.sqlite (backed up, then written to)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything")
    args = parser.parse_args()

    source, dest = Path(args.source), Path(args.dest)
    if not source.exists():
        print(f"Source database not found: {source}", file=sys.stderr)
        return 1
    if not dest.exists():
        print(f"Destination database not found: {dest}", file=sys.stderr)
        return 1

    if not args.dry_run:
        confirm = input(
            f"This will back up, then modify:\n  {dest}\nusing metadata from:\n  {source}\n"
            "Make sure Mixxx is closed on both sides. Continue? [y/N] "
        )
        if confirm.strip().lower() != "y":
            print("Aborted; nothing was touched.")
            return 1

    try:
        report = migrate(source, dest, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Migration failed and was rolled back (destination is otherwise unchanged): {exc}", file=sys.stderr)
        return 1

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Matched {report.matched_count} track(s) by filename + duration.")
    print(f"{prefix}Cues/hot cues copied: {report.cues_copied}")
    print(f"{prefix}Crate memberships copied: {report.crate_memberships_copied}")
    if report.crates_created:
        print(f"{prefix}Crates created on destination: {', '.join(report.crates_created)}")
    if report.unmatched_source_filenames:
        print(
            f"\n{len(report.unmatched_source_filenames)} source track(s) had no confident match on the "
            "destination (filename missing there, or ambiguous among same-named files) -- handle these manually:"
        )
        for name in report.unmatched_source_filenames:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
