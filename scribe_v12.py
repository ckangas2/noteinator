import os
import re
import json
import time
import queue
import sqlite3
import logging
import threading
from datetime import datetime
from pathlib import Path

import requests
from asr import load_transcriber
from uploader import start_upload_server
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Noteinator")

# --- CONFIG ---
BASE_DIR = Path(os.getenv("NOTEINATOR_BASE_DIR", os.getcwd()))
WATCH_FOLDER = BASE_DIR / "incoming_audio"
ARCHIVE_FOLDER = BASE_DIR / "processed_audio"
FAILED_FOLDER = BASE_DIR / "failed_audio"
DB_FILE = BASE_DIR / "lab_notebook.db"

# ASR backend is chosen in asr.py via NOTEINATOR_ASR (parakeet | canary | faster-whisper)

# Direct upload from phone (leave token unset to disable the upload server)
UPLOAD_TOKEN = os.getenv("NOTEINATOR_UPLOAD_TOKEN", "")
UPLOAD_HOST = os.getenv("NOTEINATOR_UPLOAD_HOST", "0.0.0.0")  # ideally your Tailscale IP
UPLOAD_PORT = int(os.getenv("NOTEINATOR_UPLOAD_PORT", "8765"))
MAX_UPLOAD_MB = int(os.getenv("NOTEINATOR_MAX_UPLOAD_MB", "200"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "")  # must match `ollama list` exactly
LLM_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

AUDIO_EXTS = {'.m4a', '.wav', '.mp3'}
CATEGORIES = ["Observation", "Data", "Idea", "Protocol", "ToDo", "Maintenance"]
_CATEGORY_LOOKUP = {re.sub(r'[^a-z]', '', c.lower()): c for c in CATEGORIES}

STABLE_SECONDS = 2      # file size must be unchanged this long before processing
STABLE_TIMEOUT = 120    # give up waiting on a file that never stops growing


# --- DB INIT (creates or migrates) ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS lab_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    category TEXT DEFAULT 'General',
                    content TEXT NOT NULL,
                    raw_transcript TEXT
                 )''')
    # v11 additions: safe to run on an existing v10 database
    existing = {row[1] for row in c.execute("PRAGMA table_info(lab_notes)")}
    if "recorded_at" not in existing:
        c.execute("ALTER TABLE lab_notes ADD COLUMN recorded_at TEXT")
        logger.info("[DB] Added column recorded_at")
    if "audio_file" not in existing:
        c.execute("ALTER TABLE lab_notes ADD COLUMN audio_file TEXT")
        logger.info("[DB] Added column audio_file")
    conn.commit()
    conn.close()


# --- THE BRAIN (LLM Extraction) ---
def normalize_category(raw) -> str:
    key = re.sub(r'[^a-z]', '', str(raw or '').lower())
    return _CATEGORY_LOOKUP.get(key, "General")


def extract_json_blob(text: str):
    """Pull a JSON object out of a model response that may be wrapped in
    reasoning text, <think> blocks, or markdown fences."""
    if not text or not text.strip():
        raise ValueError("model returned an empty response")
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'^```(?:json)?|```$', '', cleaned.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} or [...] in the text
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in model response: {cleaned[:200]!r}")


def llm_extract_notes(raw_text):
    logger.info("[Brain] Extracting structured notes...")
    system_prompt = f"""
    You are a Lab Assistant. Analyze the user's speech and extract distinct entries.
    Only include information the user actually said; do not invent details.
    Respond with JSON only, no explanation or reasoning.
    Output a JSON object with a key "entries" containing a list of items.
    Each item must have:
    - "category": Choose ONE of {CATEGORIES}
    - "content": A clear, professional summary of the point.
    """
    payload = {
        "model": MODEL_NAME,
        "prompt": f"{system_prompt}\n\nUSER INPUT: {raw_text}",
        "stream": False,
        "format": "json",
        "think": False,          # Qwen3 / reasoning models: skip the thinking pass
        "options": {"temperature": 0.2},
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=LLM_TIMEOUT)
        if response.status_code == 400 and "think" in response.text.lower():
            payload.pop("think")  # older Ollama, or a model without a thinking mode
            response = requests.post(OLLAMA_URL, json=payload, timeout=LLM_TIMEOUT)
        response.raise_for_status()
        result = extract_json_blob(response.json().get("response", ""))

        if isinstance(result, dict) and "entries" in result:
            items = result["entries"]
        elif isinstance(result, list):
            items = result
        else:
            items = [result] if result else []

        entries = []
        for item in items:
            if isinstance(item, dict) and str(item.get("content", "")).strip():
                entries.append({
                    "category": normalize_category(item.get("category")),
                    "content": str(item["content"]).strip(),
                })
        if not entries:
            raise ValueError("LLM returned no usable entries")
        return entries
    except Exception as e:
        logger.error(f"[Brain Error] {e} -- saving raw transcript as General")
        return [{"category": "General", "content": raw_text}]


# --- THE WRITER ---
def save_notes(entries, raw_text, recorded_at, audio_file) -> bool:
    try:
        conn = sqlite3.connect(DB_FILE)
        with conn:  # single transaction: all entries or none
            for note in entries:
                conn.execute(
                    "INSERT INTO lab_notes (category, content, raw_transcript, recorded_at, audio_file) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (note["category"], note["content"], raw_text, recorded_at, audio_file))
                logger.info(f"[Saved] [{note['category']}] {note['content'][:40]}...")
        conn.close()
        return True
    except Exception as e:
        logger.error(f"[DB Error] {e}")
        return False


# --- FILE HELPERS ---
def is_candidate(path: Path) -> bool:
    name = path.name
    if name.startswith('.') or name.startswith('~syncthing~') or name.endswith('.tmp'):
        return False  # Syncthing / editor temp files
    return path.suffix.lower() in AUDIO_EXTS


def wait_until_stable(path: Path) -> bool:
    """Wait until the file stops growing (sync finished)."""
    deadline = time.time() + STABLE_TIMEOUT
    last_size, stable_since = -1, time.time()
    while time.time() < deadline:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size != last_size:
            last_size, stable_since = size, time.time()
        elif size > 0 and time.time() - stable_since >= STABLE_SECONDS:
            return True
        time.sleep(0.5)
    return False


def unique_dest(folder: Path, name: str) -> Path:
    dest = folder / f"{int(time.time())}_{name}"
    n = 1
    while dest.exists():
        dest = folder / f"{int(time.time())}_{n}_{name}"
        n += 1
    return dest


# --- THE WORKER (one file at a time, off the watchdog thread) ---
class Worker(threading.Thread):
    def __init__(self, transcriber):
        super().__init__(daemon=True)
        self.transcriber = transcriber
        self.q = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()

    def enqueue(self, path: Path):
        if not is_candidate(path):
            return
        key = str(path.resolve())
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.q.put(path)

    def run(self):
        while True:
            path = self.q.get()
            try:
                self.process(path)
            finally:
                with self.lock:
                    self.pending.discard(str(path.resolve()))

    def process(self, path: Path):
        if not wait_until_stable(path):
            logger.warning(f"[Ear] {path.name} vanished or never finished syncing; skipping")
            return

        logger.info(f"[Ear] Processing: {path.name}")
        # Phone-side mtime is preserved by Syncthing, so this is ~when you recorded it
        recorded_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec='seconds')

        try:
            text = self.transcriber.transcribe(path)
        except Exception as e:
            logger.error(f"[Processing Error] {path.name}: {e}")
            self.move(path, FAILED_FOLDER, "Failed")
            return

        if len(text) <= 5:
            logger.warning(f"[Scribe] Audio too short or silent: {path.name}")
            self.move(path, FAILED_FOLDER, "Failed")
            return

        logger.info(f"[Scribe] Heard: {text[:60]}...")
        entries = llm_extract_notes(text)
        archive_path = unique_dest(ARCHIVE_FOLDER, path.name)

        if save_notes(entries, text, recorded_at, archive_path.name):
            self.move(path, archive_path.parent, "Archive", dest=archive_path)
        else:
            self.move(path, FAILED_FOLDER, "Failed")

    @staticmethod
    def move(path: Path, folder: Path, label: str, dest: Path = None):
        dest = dest or unique_dest(folder, path.name)
        try:
            path.rename(dest)
            logger.info(f"[{label}] Moved to {dest.relative_to(BASE_DIR)}")
        except Exception as e:
            logger.error(f"[Move Error] {path.name}: {e}")


# --- THE WATCHER ---
class ScribeHandler(FileSystemEventHandler):
    def __init__(self, worker):
        self.worker = worker

    def on_created(self, event):
        if not event.is_directory:
            self.worker.enqueue(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.worker.enqueue(Path(event.src_path))

    def on_moved(self, event):
        # Syncthing writes a temp file then renames it to the real name
        if not event.is_directory:
            dest = Path(event.dest_path)
            if dest.parent.resolve() == WATCH_FOLDER.resolve():
                self.worker.enqueue(dest)


if __name__ == "__main__":
    for folder in (WATCH_FOLDER, ARCHIVE_FOLDER, FAILED_FOLDER):
        folder.mkdir(parents=True, exist_ok=True)
    init_db()

    logger.info("--- NOTEINATOR V12 ---")
    logger.info(f"Watching: {WATCH_FOLDER}")
    if not MODEL_NAME:
        logger.error("OLLAMA_MODEL is not set. Pick one from `ollama list` "
                     "(the exact name including the tag) and set it in noteinator.env.")
        raise SystemExit(1)
    logger.info(f"LLM: {MODEL_NAME} via {OLLAMA_URL}")

    worker = Worker(load_transcriber())
    worker.start()

    if UPLOAD_TOKEN:
        start_upload_server(UPLOAD_HOST, UPLOAD_PORT, UPLOAD_TOKEN, WATCH_FOLDER,
                            AUDIO_EXTS, MAX_UPLOAD_MB)
    else:
        logger.warning("[Upload] NOTEINATOR_UPLOAD_TOKEN not set; direct upload disabled")

    # Pick up anything that arrived while we were offline
    backlog = sorted(p for p in WATCH_FOLDER.iterdir() if p.is_file() and is_candidate(p))
    if backlog:
        logger.info(f"[Startup] Queuing {len(backlog)} file(s) already in the inbox")
    for p in backlog:
        worker.enqueue(p)

    observer = Observer()
    observer.schedule(ScribeHandler(worker), str(WATCH_FOLDER), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down Noteinator...")
        observer.stop()
        observer.join()
