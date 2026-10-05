# 🎙️ Noteinator

**License:** MIT

Noteinator automates the pipeline from voice recording → transcription → database entry. It's designed for lab environments where you can't type in real-time but need a searchable record of what happened.

## 🎯 Why this exists

Typing notes while wearing PPE or handling samples is impossible. Traditionally, this means either relying on memory (which fails) or recording voice memos that just sit in a folder and never get reviewed.

Noteinator turns those audio files into structured data automatically so you can actually query your logs later.

## 🏗️ How it works

Noteinator runs as a background service on the lab server:

1. **Capture:** an iOS Shortcut records audio and POSTs it straight to the server over Tailscale. No app to open; one tap on the Action Button or "Hey Siri, lab note".
2. **Transcription:** NVIDIA Parakeet (`parakeet-tdt-0.6b-v2`) runs locally on the GPU.
3. **Parsing:** the transcript goes to a local Ollama instance (`llama3.2`) which extracts distinct entries (Observation, Data, Idea, Protocol, ToDo, Maintenance) and strips filler words.
4. **Storage:** entries are written to SQLite, each linked back to its archived audio file.

A folder watcher also runs, so dropping a file into `incoming_audio/` by any means (Syncthing, `scp`, drag and drop) still works.

The transcription model loads on first use and unloads after 5 idle minutes
(`NOTEINATOR_IDLE_UNLOAD_MINUTES`), so a mostly-idle service holds no GPU memory. The
first note after a quiet spell costs about 10 seconds of reload.

## 💻 Tech Stack

- **Transcription:** `parakeet-tdt-0.6b-v2` via NeMo (swappable: Canary-Qwen 2.5B or faster-whisper)
- **Parsing:** `Ollama / Llama 3.2` (Local)
- **Capture:** iOS Shortcut → HTTP upload over Tailscale
- **Trigger:** `watchdog` (Filesystem events)
- **Database:** `SQLite`

## 📁 Repo Structure

```
├── scribe_v12.py     # Main service: watcher, worker, LLM extraction, DB
├── asr.py            # Speech-to-text backends (parakeet / canary / faster-whisper)
├── uploader.py       # HTTP upload endpoint for direct phone capture
├── compare_asr.py    # Benchmark backends against your own recordings
├── backfill.py       # Re-process an archive of old recordings
├── stats.py          # Summary stats for the notebook
├── R/                # Analysis and dashboard code
├── incoming_audio/   # Where new recordings land
├── processed_audio/  # Archive of what's been transcribed
├── failed_audio/     # Recordings that failed transcription
└── lab_notebook.db   # The database
```

## 🚀 Setup

### 1. Dependencies

```bash
sudo apt install ffmpeg
pip install -r requirements.txt
ollama pull llama3.2
```

### 2. Configure

```bash
cp noteinator.env.example noteinator.env
# generate an upload token
openssl rand -hex 24
# find your Tailscale IP to bind to
tailscale ip -4
```

Edit `noteinator.env` with those values.

### 3. Run

```bash
set -a && source noteinator.env && set +a
python scribe_v12.py
```

Check it's up from another machine on your tailnet:

```bash
curl http://<tailscale-ip>:8765/health
```

### 4. Run as a service (optional)

```bash
cp noteinator.env ~/noteinator.env
mkdir -p ~/.config/systemd/user && cp noteinator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now noteinator
systemctl --user status noteinator
journalctl --user -u noteinator -f
```

To keep it running while you're logged out: `sudo loginctl enable-linger $USER`

## 📱 iOS Shortcut

Install Tailscale on the phone and sign in to the same tailnet. Then scan the QR code
the service prints at startup (`pip install qrcode` if you don't see one) — it opens a
setup page with a single tap-to-copy upload URL (the token is in the query string), so
there's nothing to type by hand. The page also lists the Shortcut steps:

1. **Record Audio** — Start Recording: Immediately, Finish Recording: On Tap
2. **Save File** — to a Files folder like `Noteinator/`, Ask Where to Save off (offline backup)
3. **Format Date** — Current Date, ISO 8601, include time
4. **Get Contents of URL**
   - URL: paste from the setup page (`http://<tailscale-ip>:8765/upload?t=<token>`)
   - Method: `POST`
   - Header: `X-Recorded-At: <Formatted Date>`
   - Request Body: **File** (not Form) → Recorded Audio
5. **Show Notification** — show the result so you get confirmation

Assign it to the Action Button or Back Tap, or name it "Lab Note" to trigger with Siri.

If an upload fails, the recording is still saved in Files and can be sent later.

### Sharing the Shortcut with others

Once it works, share it so nobody else has to rebuild it:

1. In Shortcuts, open the **Get Contents of URL** action and tap the URL field's
   **Import Question** option. Phrase it like "What's your Noteinator upload URL?"
   This keeps your own token out of the shared copy.
2. Share → Copy iCloud Link, and put that link in your lab's docs.

Setup for everyone else then becomes: install Tailscale, tap the link, scan the QR code
from the server, paste one value.

The token travels in the URL query string, which is fine on a private tailnet but means
it can appear in proxy or browser logs. The `Authorization: Bearer <token>` header is
still accepted and is the better choice if you ever expose this beyond Tailscale.

## 🔧 Configuration

| Variable | Default | Notes |
|---|---|---|
| `NOTEINATOR_BASE_DIR` | cwd | Where audio folders and the DB live |
| `NOTEINATOR_ASR` | `parakeet` | `parakeet`, `canary`, or `faster-whisper` |
| `NOTEINATOR_IDLE_UNLOAD_MINUTES` | `5` | Free GPU memory after this long with no recordings; `0` keeps the model resident. Reloading costs ~10s on the next note. |
| `NOTEINATOR_UPLOAD_TOKEN` | — | Unset disables the upload server |
| `NOTEINATOR_UPLOAD_HOST` | `0.0.0.0` | Set to your Tailscale IP |
| `NOTEINATOR_UPLOAD_PORT` | `8765` | |
| `NOTEINATOR_MAX_UPLOAD_MB` | `200` | |
| `NOTEINATOR_VOCAB` | — | Domain terms; used by `canary` and `faster-whisper` only (Parakeet has no prompt input) |
| `WHISPER_MODEL_SIZE` | `small.en` | faster-whisper only |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/generate` | |
| `OLLAMA_MODEL` | `llama3.2` | Must match `ollama list` exactly, tag included |
| `OLLAMA_TIMEOUT` | `180` | Seconds to wait for note extraction |
| `PARAKEET_MODEL` | `nvidia/parakeet-tdt-0.6b-v2` | `-v3` is the multilingual version |
| `CANARY_MODEL` | `nvidia/canary-qwen-2.5b` | |
| `WHISPER_VOCAB_PROMPT` | — | Overrides `NOTEINATOR_VOCAB` for faster-whisper |
| `NOTEINATOR_VERBOSE` | — | `1` restores NeMo's full logging for debugging |
| `NOTEINATOR_QR_ASCII` | — | `1` draws the setup QR with block characters instead of ANSI colours |

## 🔬 Choosing a transcription model

`compare_asr.py` runs several backends over your own recordings and writes a side-by-side report:

```bash
python compare_asr.py processed_audio/ --backends parakeet canary faster-whisper --limit 12
```

Parakeet is the default: quality on lab speech matched Canary-Qwen 2.5B in testing, at a quarter of the parameters, which leaves GPU headroom for Ollama. Canary accepts a vocabulary prompt (see `NOTEINATOR_VOCAB`), which Parakeet cannot; worth revisiting if domain terms turn out to be the limiting factor.

## 📊 Checking your notebook

```bash
python stats.py              # totals, categories, when you record, recent ToDos
python stats.py --days 30    # just the last month
```

The figure to watch is the share of notes filed as `General`: those are recordings
where LLM extraction failed and the raw transcript was saved instead. A few percent is
normal; a lot means the model or prompt needs attention.

## 📥 Backfilling old recordings

To run an existing archive of audio through the current pipeline:

```bash
python backfill.py /path/to/old/recordings --dest $NOTEINATOR_BASE_DIR/incoming_audio --dry-run
python backfill.py /path/to/old/recordings --dest $NOTEINATOR_BASE_DIR/incoming_audio --limit 5
```

Source files are copied, never modified. It strips the archive prefix scribe_v10 added
and recovers the real recording time from names like
`Audio Recording 2026-02-12 at 6.24.09 PM.m4a`, so `recorded_at` reflects when you spoke
rather than when the files were last copied around. Files whose names carry no date fall
back to their mtime. Always `--dry-run` first.

## 🗄️ Database

```sql
CREATE TABLE lab_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP,  -- when processed (UTC)
    recorded_at    TEXT,                                -- when spoken (ISO 8601, local offset)
    category       TEXT DEFAULT 'General',
    content        TEXT NOT NULL,
    raw_transcript TEXT,
    audio_file     TEXT                                 -- filename in processed_audio/
);
```

`recorded_at` and `audio_file` are added automatically to existing databases on first run.

## 📜 License

MIT. See LICENSE.