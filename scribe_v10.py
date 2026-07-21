import time
import os
import json
import requests
import sqlite3
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from faster_whisper import WhisperModel

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Noteinator")

# --- CONFIG (Now using Pathlib and Environment Variables) ---
# Defaults to current directory if not specified in environment
BASE_DIR = Path(os.getenv("NOTEINATOR_BASE_DIR", os.getcwd()))
WATCH_FOLDER = BASE_DIR / "incoming_audio"
ARCHIVE_FOLDER = BASE_DIR / "processed_audio"
DB_FILE = BASE_DIR / "lab_notebook.db"

MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small.en")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "llama3.2")

# --- DB INIT ---
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
    conn.commit()
    conn.close()

# --- THE BRAIN (LLM Extraction) ---
def llm_extract_notes(raw_text):
    logger.info("[Brain] Extracting structured notes...")
    
    system_prompt = """
    You are a Lab Assistant. Analyze the user's speech and extract distinct entries.
    
    Output a JSON object with a key "entries" containing a list of items.
    Each item must have:
    - "category": Choose ONE [Observation, Data, Idea, Protocol, ToDo, Maintenance]
    - "content": A clear, professional summary of the point.
    """
    
    payload = {
        "model": MODEL_NAME,
        "prompt": f"{system_prompt}\n\nUSER INPUT: {raw_text}",
        "stream": False,
        "format": "json"
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=45)
        response.raise_for_status()
        result = json.loads(response.json()['response'])
        
        if isinstance(result, dict) and "entries" in result: 
            return result["entries"]
        elif isinstance(result, list): 
            return result
        else: 
            return [result] if result else []
        
    except Exception as e:
        logger.error(f"[Brain Error] {e}")
        return [{ "category": "General", "content": raw_text }]

# --- THE WRITER ---
def save_notes(entries, raw_text):
    if not entries: return
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for note in entries:
            cat = note.get('category', 'General').capitalize()
            content = note.get('content', 'No Content')
            c.execute("INSERT INTO lab_notes (category, content, raw_transcript) VALUES (?, ?, ?)",
                     (cat, content, raw_text))
            logger.info(f"[Saved] [{cat}] {content[:40]}...")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB Error] {e}")

# --- THE EAR (Folder Watcher) ---
class ScribeHandler(FileSystemEventHandler):
    def __init__(self, model):
        self.model = model
        self.last_processed = {}

    def _process(self, filepath):
        path = Path(filepath)
        if path.suffix.lower() in ('.m4a', '.wav', '.mp3'):
            # Debounce to avoid processing partially written files
            if time.time() - self.last_processed.get(str(path), 0) < 2: return
            self.last_processed[str(path)] = time.time()
            
            logger.info(f"[Ear] Processing: {path.name}")
            # Small sleep to ensure file is fully written/unlocked by OS
            time.sleep(1)
            
            try:
                segments, _ = self.model.transcribe(str(path), beam_size=5)
                text = " ".join([s.text for s in segments]).strip()
                
                if len(text) > 5:
                    logger.info(f"[Scribe] Heard: {text[:60]}...")
                    entries = llm_extract_notes(text)
                    save_notes(entries, text)
                    
                    # Archive file with timestamp
                    archive_path = ARCHIVE_FOLDER / f"{int(time.time())}_{path.name}"
                    path.rename(archive_path)
                    logger.info(f"[Archive] Moved to {archive_path.name}")
                else:
                    logger.warning(f"[Scribe] Audio too short or silent: {path.name}")
            except Exception as e: 
                logger.error(f"[Processing Error] {e}")

    def on_created(self, event): 
        if not event.is_directory: self._process(event.src_path)
    def on_modified(self, event): 
        if not event.is_directory: self._process(event.src_path)

if __name__ == "__main__":
    init_db()
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)
    
    logger.info("--- NOTEINATOR V10: INDUSTRIAL MODE ---")
    logger.info(f"Watching: {WATCH_FOLDER}")
    logger.info(f"Archiving to: {ARCHIVE_FOLDER}")
    logger.info(f"Using Model: {MODEL_NAME} via {OLLAMA_URL}")

    # Auto-detect GPU (CUDA) otherwise CPU
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    logger.info(f"Hardware acceleration: {device} ({compute_type})")

    model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)
    
    observer = Observer()
    observer.schedule(ScribeHandler(model), str(WATCH_FOLDER), recursive=False)
    observer.start()
    
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        logger.info("Shutting down Noteinator...")
        observer.stop()
    observer.join()
