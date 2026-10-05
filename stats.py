"""Summary stats for the Noteinator database.

  python stats.py                       # uses NOTEINATOR_BASE_DIR
  python stats.py --db path/to.db
  python stats.py --days 30             # only the last 30 days
"""
import argparse
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

CATEGORIES = ["Observation", "Data", "Idea", "Protocol", "ToDo", "Maintenance", "General"]


def note_time(row):
    """Best available timestamp: recorded_at if set, else the processing timestamp."""
    raw = row["recorded_at"] or row["timestamp"]
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def bar(n, total, width=28):
    filled = round(width * n / total) if total else 0
    return "\u2588" * filled + "\u2591" * (width - filled)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path)
    ap.add_argument("--days", type=int, help="only notes from the last N days")
    ap.add_argument("--recent", type=int, default=5, help="how many recent notes to show")
    args = ap.parse_args()

    db = args.db or Path(os.getenv("NOTEINATOR_BASE_DIR", ".")) / "lab_notebook.db"
    if not db.exists():
        raise SystemExit(f"No database at {db}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_notes)")}
    for missing in ("recorded_at", "audio_file"):
        if missing not in cols:
            print(f"note: this database has no {missing} column (pre-v11)\n")
    select = "SELECT id, category, content, raw_transcript, timestamp, " + \
             ("recorded_at" if "recorded_at" in cols else "NULL AS recorded_at") + ", " + \
             ("audio_file" if "audio_file" in cols else "NULL AS audio_file") + \
             " FROM lab_notes"
    rows = list(conn.execute(select))
    conn.close()

    if args.days:
        cutoff = datetime.now() - timedelta(days=args.days)
        rows = [r for r in rows if (t := note_time(r)) and t >= cutoff]
    if not rows:
        raise SystemExit("No notes found" + (f" in the last {args.days} days" if args.days else ""))

    times = sorted(t for t in (note_time(r) for r in rows) if t)
    recordings = {r["audio_file"] for r in rows if r["audio_file"]}

    print(f"\n\033[1mNoteinator\033[0m  {db}")
    print("=" * 56)
    print(f"  {len(rows):>6} notes")
    if recordings:
        print(f"  {len(recordings):>6} recordings  ({len(rows)/len(recordings):.1f} notes each)")
    if times:
        span = (times[-1] - times[0]).days + 1
        print(f"  {span:>6} days covered  ({times[0]:%b %d %Y} to {times[-1]:%b %d %Y})")
        active = len({t.date() for t in times})
        print(f"  {active:>6} days with notes  ({len(rows)/active:.1f} per active day)")

    # --- categories ---
    counts = Counter(r["category"] for r in rows)
    print("\n\033[1mBy category\033[0m")
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / len(rows)
        print(f"  {cat:<13} {n:>5}  {bar(n, len(rows))} {pct:4.1f}%")
    if counts.get("General"):
        print(f"\n  \033[33mGeneral = LLM extraction fell back to the raw transcript.\033[0m")
        print(f"  \033[33m{counts['General']} of {len(rows)} ({100*counts['General']/len(rows):.0f}%).\033[0m")

    # --- activity over time ---
    if times:
        months = Counter((t.year, t.month) for t in times)
        first, last = times[0], times[-1]
        span_months = (last.year - first.year) * 12 + last.month - first.month + 1
        peak = max(months.values())
        print("\n\033[1mNotes per month\033[0m")
        y, m = first.year, first.month
        gaps = 0
        for _ in range(span_months):
            n = months.get((y, m), 0)
            label = datetime(y, m, 1).strftime("%b %Y")
            if n:
                print(f"  {label}  {n:>4}  {bar(n, peak)}")
            else:
                gaps += 1
                print(f"  {label}     -  \033[2m{'\u00b7' * 28}\033[0m")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if gaps:
            print(f"\n  \033[33m{gaps} month(s) with no notes. If that is not right, those\033[0m")
            print(f"  \033[33mrecordings may be missing from the archive you backfilled.\033[0m")

        # busiest individual days
        by_day = Counter(t.date() for t in times)
        print("\n\033[1mBusiest days\033[0m")
        for day, n in by_day.most_common(5):
            print(f"  {day:%a %b %d %Y}  {n:>4}  {bar(n, by_day.most_common(1)[0][1])}")

    # --- when do you record? ---
    if times:
        print("\n\033[1mBy hour of day\033[0m")
        hours = Counter(t.hour for t in times)
        peak = max(hours.values())
        for h in range(0, 24, 2):
            n = hours[h] + hours[h + 1]
            if n or 6 <= h <= 20:
                label = f"{h:02d}-{h+2:02d}"
                print(f"  {label}  {n:>4}  {bar(n, peak * 2)}")

        print("\n\033[1mBy weekday\033[0m")
        days = Counter(t.strftime("%a") for t in times)
        for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            print(f"  {d}  {days[d]:>4}  {bar(days[d], max(days.values()) if days else 1)}")

    # --- length ---
    lengths = [len(r["content"] or "") for r in rows]
    transcripts = [len(r["raw_transcript"] or "") for r in rows if r["raw_transcript"]]
    print("\n\033[1mNote length (characters)\033[0m")
    print(f"  shortest {min(lengths)},  median {sorted(lengths)[len(lengths)//2]},  longest {max(lengths)}")
    if transcripts:
        ratio = sum(lengths) / sum(transcripts)
        print(f"  summaries are {ratio:.0%} the length of the raw transcripts")

    # --- open todos ---
    todos = [r for r in rows if r["category"] == "ToDo"]
    if todos:
        print(f"\n\033[1mMost recent ToDos\033[0m")
        todos.sort(key=lambda r: note_time(r) or datetime.min, reverse=True)
        for r in todos[:args.recent]:
            t = note_time(r)
            when = f"{t:%b %d}" if t else "  ?  "
            print(f"  {when}  {r['content'][:66]}")

    print(f"\n\033[1mMost recent notes\033[0m")
    rows.sort(key=lambda r: note_time(r) or datetime.min, reverse=True)
    for r in rows[:args.recent]:
        t = note_time(r)
        when = f"{t:%b %d %H:%M}" if t else "   ?   "
        print(f"  {when}  [{r['category']}] {r['content'][:52]}")
    print()


if __name__ == "__main__":
    main()
