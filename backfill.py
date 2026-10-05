"""Re-process an archive of old recordings through the current pipeline.

Copies audio into the watch folder. Source files are opened read-only and never
renamed, moved or modified.

  python backfill.py ~/old/processed_audio --dest ~/noteinator-data/incoming_audio
  python backfill.py ~/old/processed_audio --dest ... --limit 5        # try a few first
  python backfill.py ~/old/processed_audio --dest ... --dry-run        # show, change nothing

Two things it fixes along the way:
  * strips the `1770942320_` epoch prefix that scribe_v10 added when archiving
  * recovers the real recording time from names like
    "Audio Recording 2026-02-12 at 6.24.09 PM.m4a" and sets it as the file's mtime,
    which is what the pipeline stores as recorded_at. Without this every backfilled
    note is stamped with whenever the files happened to be copied around.
"""
import argparse
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

AUDIO_EXTS = {'.m4a', '.wav', '.mp3', '.flac'}
ARCHIVE_PREFIX = re.compile(r'^\d{9,11}_')          # scribe_v10/v12 archive prefix
UPLOAD_PREFIX = re.compile(r'^\d{8}-\d{6}_')        # uploader prefix

# "Audio Recording 2026-02-12 at 6.24.09 PM" / "... at 6.24.09 AM"
IOS_NAME = re.compile(
    r'(?P<date>\d{4}-\d{2}-\d{2})\s+at\s+(?P<h>\d{1,2})\.(?P<m>\d{2})\.(?P<s>\d{2})\s*(?P<ampm>[AP]M)',
    re.IGNORECASE)


def clean_name(name: str) -> str:
    stem = ARCHIVE_PREFIX.sub('', name)
    return UPLOAD_PREFIX.sub('', stem)


def recorded_time(name: str):
    """Recording time parsed out of the filename, or None."""
    m = IOS_NAME.search(name)
    if not m:
        return None
    hour = int(m.group('h')) % 12
    if m.group('ampm').upper() == 'PM':
        hour += 12
    try:
        return datetime.strptime(
            f"{m.group('date')} {hour:02d}:{m.group('m')}:{m.group('s')}",
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path, help="folder of old recordings (read only)")
    ap.add_argument("--dest", type=Path, required=True, help="incoming_audio folder")
    ap.add_argument("--limit", type=int, help="only do this many (oldest first)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between files, so the worker isn't swamped (default 3)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    source, dest = args.source.expanduser().resolve(), args.dest.expanduser().resolve()
    if not source.is_dir():
        sys.exit(f"Source folder not found: {source}")
    if source == dest:
        sys.exit("Source and destination are the same folder; refusing to run.")
    if not args.dry_run and not dest.is_dir():
        sys.exit(f"Destination folder not found: {dest}")

    files = sorted(p for p in source.iterdir()
                   if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit(f"No audio files found in {source}")

    print(f"{len(files)} file(s) from {source}")
    print(f"           -> {dest}{'  (DRY RUN)' if args.dry_run else ''}\n")

    copied = undated = 0
    for src in files:
        name = clean_name(src.name)
        when = recorded_time(name)
        target = dest / name
        n = 1
        while target.exists() or (not args.dry_run and target.exists()):
            target = dest / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1

        stamp = when.strftime('%Y-%m-%d %H:%M') if when else "no date in name"
        print(f"  {src.name}\n    -> {target.name}   [{stamp}]")
        if when is None:
            undated += 1

        if not args.dry_run:
            shutil.copy2(src, target)  # copy2 keeps the source untouched
            if when:
                ts = when.timestamp()
                import os
                os.utime(target, (ts, ts))
            copied += 1
            time.sleep(args.delay)

    print()
    if args.dry_run:
        print("Dry run: nothing was copied.")
    else:
        print(f"Copied {copied} file(s). Source folder untouched.")
    if undated:
        print(f"{undated} file(s) had no date in the name; they keep the copy time "
              f"as recorded_at.")


if __name__ == "__main__":
    main()
