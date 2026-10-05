"""Compare ASR backends on your own recordings.

Usage:
  python compare_asr.py processed_audio/ --backends parakeet canary faster-whisper --limit 12

Writes asr_comparison.md with each file's transcript from every backend, plus timing.
Loads one model at a time to keep GPU memory down.
"""
import argparse
import gc
import logging
import time
from pathlib import Path

from asr import load_transcriber

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
AUDIO_EXTS = {'.m4a', '.wav', '.mp3', '.flac'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--backends", nargs="+", default=["parakeet", "canary", "faster-whisper"])
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--out", type=Path, default=Path("asr_comparison.md"))
    args = ap.parse_args()

    files = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in AUDIO_EXTS)[-args.limit:]
    if not files:
        raise SystemExit(f"No audio files in {args.folder}")

    results = {f.name: {} for f in files}
    timing = {}
    for name in args.backends:
        print(f"\n=== {name} ===")
        t0 = time.time()
        try:
            model = load_transcriber(name, idle_unload_minutes=0)
        except Exception as e:
            print(f"  !! could not load {name}: {e}")
            print("     skipping it; other backends will still run")
            for f in files:
                results[f.name][name] = f"LOAD FAILED: {e}"
            continue
        load_s = time.time() - t0
        t1 = time.time()
        for f in files:
            try:
                results[f.name][name] = model.transcribe(f)
            except Exception as e:
                results[f.name][name] = f"ERROR: {e}"
            print(f"  {f.name}: {results[f.name][name][:70]}")
        timing[name] = (load_s, time.time() - t1)
        write_report(args.out, args.folder, files, results, timing)
        del model
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass

    write_report(args.out, args.folder, files, results, timing)
    print(f"\nWrote {args.out}")


def write_report(out, folder, files, results, timing):
    lines = [f"# ASR comparison\n\n_{len(files)} files from `{folder}`_\n", "| Backend | Load (s) | Transcribe all (s) |", "|---|---|---|"]
    lines += [f"| {n} | {l:.1f} | {t:.1f} |" for n, (l, t) in timing.items()]
    for fname in (f.name for f in files):
        by_backend = results[fname]
        lines.append(f"\n## {fname}\n")
        for n, text in by_backend.items():
            lines.append(f"**{n}:** {text}\n")
    out.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
