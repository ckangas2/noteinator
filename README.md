# Noteinator

Talk into your phone, get a searchable lab notebook.

You can't type while wearing gloves or holding a plate. So you either try to remember
what happened and write it up later (you won't), or you record voice memos that pile up
in a folder and never get listened to again. Noteinator takes those recordings and turns
them into structured database entries without you doing anything.

## How it works

One tap on the phone records a note and sends it to the lab server over Tailscale. On the
server, Parakeet transcribes it on the GPU, a local LLM pulls out the distinct points and
files each under a category, and the results go into SQLite with a link back to the
original audio. Nothing leaves the tailnet.

Categories are Observation, Data, Idea, Protocol, ToDo and Maintenance. One rambling
recording usually becomes two or three entries — "I need to infect the pancreatic cancer
cells tomorrow and also order primers" is two ToDos, not one note.

A folder watcher runs alongside the upload endpoint, so dropping files into
`incoming_audio/` by any other route (Syncthing, `scp`, whatever) works too.

The transcription model loads when it's first needed and unloads after five idle minutes,
so a service that sits quiet all afternoon isn't holding GPU memory. The first note after
a quiet spell takes about ten seconds longer while it reloads.

## What it's built on

- **Transcription:** `parakeet-tdt-0.6b-v2` via NeMo. Canary-Qwen 2.5B and faster-whisper
  are also wired up if you want to compare.
- **Note extraction:** whatever you've got in Ollama. Anything in the 7–9B range is plenty.
- **Capture:** an iOS Shortcut posting to an HTTP endpoint over Tailscale.
- **Storage:** SQLite, because this is one person's notebook and anything else is overkill.

## Files

```
scribe_v12.py     the service: watcher, upload server, transcription, extraction, DB
asr.py            transcription backends and the idle-unload wrapper
uploader.py       upload endpoint and the phone setup page
compare_asr.py    run several ASR models over your own audio and compare
backfill.py       push an archive of old recordings through the pipeline
stats.py          what's actually in the notebook
R/                analysis and dashboard
```

## Setup

```bash
sudo apt install ffmpeg
pip install -r requirements.txt

cp noteinator.env.example noteinator.env
openssl rand -hex 24      # upload token — paste into noteinator.env
tailscale ip -4           # bind address — same
```

Set `OLLAMA_MODEL` to something `ollama list` actually shows, tag included. A name that
doesn't match returns a 404 and every note quietly lands in "General".

Start it:

```bash
set -a && source noteinator.env && set +a
python scribe_v12.py
```

From another machine on the tailnet, `curl http://<tailscale-ip>:8765/health` should come
back `{"ok": true}`.

### Running it for real

```bash
cp noteinator.env ~/noteinator.env
mkdir -p ~/.config/systemd/user && cp noteinator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now noteinator
sudo loginctl enable-linger $USER     # keeps it alive when you log out
```

Logs: `journalctl --user -u noteinator -f`

Note that systemd reads `~/noteinator.env`, not the copy in the repo. Editing the wrong
one and wondering why nothing changed is a rite of passage.

## The phone

Install Tailscale and sign into the same tailnet. Then point your camera at the QR code
the service prints on startup — it opens a page with one tap-to-copy URL, token included,
so there's nothing to type. The page also walks through the Shortcut.

The Shortcut itself is five actions:

1. **Record Audio** — Start: Immediately, Finish: On Tap
2. **Save File** — into a `Noteinator/` folder, "Ask Where to Save" off. This is your
   backup if the upload fails.
3. **Format Date** — Current Date, ISO 8601, with time
4. **Get Contents of URL** — paste the URL, method `POST`, header `X-Recorded-At` set to
   the Formatted Date, and Request Body set to **File** → Recorded Audio. It has to be
   File; the server will tell you so if you pick Form.
5. **Show Notification** — Contents of URL, so you see it worked

Name it "Lab Note" and put it on the Action Button, a Back Tap, or Siri.

If you keep Tailscale off to save battery, add toggles around the upload step and give it
a couple of seconds to connect before the POST.

### Passing it to someone else

Set the URL field as an Import Question before sharing, so your token doesn't ride along.
Then Share → Copy iCloud Link. Whoever gets it installs Tailscale, taps the link, and
pastes one value from their own QR page.

> **TODO:** add the iCloud link here once the Shortcut is published.

## Picking a transcription model

```bash
python compare_asr.py processed_audio/ --backends parakeet canary faster-whisper --limit 12
```

That writes a side-by-side report of every backend over the same files, plus timings. On
real lab speech Parakeet matched Canary at a quarter the size, which is why it's the
default.

The thing benchmarks won't tell you is how a model handles your vocabulary. Parakeet is an
RNN-T and takes no text prompt, so it can't be steered — it hears "oncolytic" correctly
most of the time and occasionally turns it into "onto". Canary and Whisper both accept a
vocabulary hint via `NOTEINATOR_VOCAB`. If domain terms turn out to be what's actually
costing you, that's the tradeoff to revisit.

## Old recordings

```bash
python backfill.py /path/to/old/audio --dest $NOTEINATOR_BASE_DIR/incoming_audio --dry-run
python backfill.py /path/to/old/audio --dest $NOTEINATOR_BASE_DIR/incoming_audio --limit 5
```

Copies, never modifies the source. It strips the archive prefix v10 used to add, and digs
the real recording time out of names like `Audio Recording 2026-02-12 at 6.24.09 PM.m4a`
— without that, every backfilled note gets stamped with whenever those files were last
copied around, which is almost never when you recorded them. Dry run first, always.

## Seeing what's in there

```bash
python stats.py
python stats.py --days 30
```

Totals, categories, when you record, how much the LLM trims, recent ToDos, and a
month-by-month histogram that makes gaps obvious.

Watch the share of notes filed as **General** — that's the fallback when extraction fails
and the raw transcript gets saved instead. Around 1% is normal and usually just short test
clips. If it climbs, check the service log.

## Configuration

| Variable | Default | |
|---|---|---|
| `NOTEINATOR_BASE_DIR` | cwd | where audio folders and the DB live |
| `NOTEINATOR_ASR` | `parakeet` | `parakeet`, `canary`, `faster-whisper` |
| `NOTEINATOR_IDLE_UNLOAD_MINUTES` | `5` | `0` keeps the model resident |
| `NOTEINATOR_UPLOAD_TOKEN` | — | unset disables the upload server entirely |
| `NOTEINATOR_UPLOAD_HOST` | `0.0.0.0` | set this to your Tailscale IP |
| `NOTEINATOR_UPLOAD_PORT` | `8765` | |
| `NOTEINATOR_MAX_UPLOAD_MB` | `200` | |
| `NOTEINATOR_VOCAB` | — | domain terms; Canary and Whisper only |
| `NOTEINATOR_VERBOSE` | — | `1` brings back NeMo's full logging |
| `NOTEINATOR_QR_ASCII` | — | `1` if your terminal mangles the ANSI QR code |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/generate` | |
| `OLLAMA_MODEL` | `llama3.2` | must match `ollama list` exactly |
| `OLLAMA_TIMEOUT` | `180` | seconds |
| `WHISPER_MODEL_SIZE` | `small.en` | faster-whisper only |
| `WHISPER_VOCAB_PROMPT` | — | overrides `NOTEINATOR_VOCAB` for Whisper |
| `PARAKEET_MODEL` | `nvidia/parakeet-tdt-0.6b-v2` | `-v3` is multilingual |
| `CANARY_MODEL` | `nvidia/canary-qwen-2.5b` | |

## Database

```sql
CREATE TABLE lab_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP,  -- when it was processed (UTC)
    recorded_at    TEXT,                                -- when you actually said it
    category       TEXT DEFAULT 'General',
    content        TEXT NOT NULL,
    raw_transcript TEXT,
    audio_file     TEXT                                 -- filename in processed_audio/
);
```

`recorded_at` and `audio_file` get added to older databases automatically on first run.
Use `recorded_at` for anything time-based; `timestamp` is processing time in UTC and will
be off by however long the file took to arrive.

## Security

The token travels in the URL query string so the Shortcut only needs one pasted value.
That's fine on a private tailnet, but query strings end up in logs in ways headers don't,
so if you ever expose this beyond Tailscale, switch to `Authorization: Bearer <token>`
— the server accepts both. Don't commit `noteinator.env`.

## Donations

This is free and will stay that way. If it saved you some time and you feel like
throwing something at it:

**BTC:** `bc1qm7ualrt9cchsu3rfr24jrhzwf2scks80qqlpr7`

<img src="docs/btc-qr.png" alt="BTC donation QR code" width="180">

## License

MIT, see [LICENSE](LICENSE).