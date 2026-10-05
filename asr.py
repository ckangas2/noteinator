"""Speech-to-text backends for Noteinator.

Pick one with NOTEINATOR_ASR = faster-whisper | parakeet | canary
Each backend exposes .transcribe(path) -> str
"""
import os
import gc
import time
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("Noteinator")

# NeMo and its deps are extremely chatty at INFO/WARNING. Keep our own logs readable.
# Set NOTEINATOR_VERBOSE=1 to see all of it again when debugging.
if os.getenv("NOTEINATOR_VERBOSE", "") != "1":
    os.environ.setdefault("NEMO_TESTING", "0")
    for noisy in ("nemo_logger", "nemo", "lhotse", "huggingface_hub", "httpx",
                  "filelock", "torch", "pytorch_lightning", "numba"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:  # NeMo keeps its own logger outside the stdlib hierarchy
        from nemo.utils import logging as nemo_logging
        nemo_logging.setLevel(logging.ERROR)
    except Exception:
        pass

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small.en")
WHISPER_VOCAB_PROMPT = os.getenv("WHISPER_VOCAB_PROMPT", "") or None
PARAKEET_MODEL = os.getenv("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v2")  # v3 = multilingual

# Lab vocabulary. Whisper and Canary accept a text prompt to bias decoding toward
# these terms; Parakeet (RNN-T) has no prompt input and ignores this.
LAB_VOCAB = os.getenv("NOTEINATOR_VOCAB", "")
CANARY_MODEL = os.getenv("CANARY_MODEL", "nvidia/canary-qwen-2.5b")
CANARY_CHUNK_SECONDS = 40  # longest audio Canary-Qwen saw in training


def to_wav16k(src: Path, workdir: Path, chunk_seconds: int = None) -> list:
    """Convert any audio to 16 kHz mono wav (what NeMo models expect).
    If chunk_seconds is set, split into consecutive chunks. Returns list of wav paths."""
    if chunk_seconds:
        pattern = workdir / "chunk_%04d.wav"
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
               "-ar", "16000", "-ac", "1", "-f", "segment",
               "-segment_time", str(chunk_seconds), str(pattern)]
    else:
        out = workdir / "audio.wav"
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
               "-ar", "16000", "-ac", "1", str(out)]
    subprocess.run(cmd, check=True)
    return sorted(str(p) for p in workdir.glob("*.wav"))


def load_wav_float32(path: str):
    """Read a 16-bit PCM wav into a float32 numpy array in [-1, 1]."""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {w.getsampwidth()*8}-bit")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self):
        from faster_whisper import WhisperModel
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"[Ear] faster-whisper {WHISPER_MODEL_SIZE} on {device} ({compute_type})")
        self.model = WhisperModel(WHISPER_MODEL_SIZE, device=device, compute_type=compute_type)

    def transcribe(self, path: Path) -> str:
        prompt = WHISPER_VOCAB_PROMPT or (LAB_VOCAB or None)
        # Decode with ffmpeg rather than letting faster-whisper use PyAV, whose
        # newer releases break faster-whisper's decoder (metadata_errors kwarg).
        with tempfile.TemporaryDirectory() as tmp:
            wav = to_wav16k(Path(path), Path(tmp))[0]
            audio = load_wav_float32(wav)
        segments, _ = self.model.transcribe(
            audio, beam_size=5, vad_filter=True, initial_prompt=prompt)
        return " ".join(s.text for s in segments).strip()


class ParakeetTranscriber:
    name = "parakeet"

    def __init__(self):
        import nemo.collections.asr as nemo_asr
        logger.info(f"[Ear] Loading {PARAKEET_MODEL} (cuda={_cuda_available()})")
        self.model = nemo_asr.models.ASRModel.from_pretrained(model_name=PARAKEET_MODEL)
        self.model.eval()

    def transcribe(self, path: Path) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            wavs = to_wav16k(Path(path), Path(tmp))
            out = self.model.transcribe(wavs, verbose=False)
        first = out[0]
        return (first.text if hasattr(first, "text") else str(first)).strip()


class CanaryQwenTranscriber:
    name = "canary"

    def __init__(self):
        import torch
        from nemo.collections.speechlm2.models import SALM
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"[Ear] Loading {CANARY_MODEL} on {device}")
        self.model = SALM.from_pretrained(CANARY_MODEL).bfloat16().eval().to(device)

    def transcribe(self, path: Path) -> str:
        import torch
        with tempfile.TemporaryDirectory() as tmp:
            chunks = to_wav16k(Path(path), Path(tmp), chunk_seconds=CANARY_CHUNK_SECONDS)
            instruction = "Transcribe the following"
            if LAB_VOCAB:
                instruction += f" (domain terms that may appear: {LAB_VOCAB})"
            prompts = [[{
                "role": "user",
                "content": f"{instruction}: {self.model.audio_locator_tag}",
                "audio": [c],
            }] for c in chunks]
            with torch.inference_mode():
                answer_ids = self.model.generate(prompts=prompts, max_new_tokens=256)
        texts = [self.model.tokenizer.ids_to_text(ids.cpu()).strip() for ids in answer_ids]
        return " ".join(t for t in texts if t).strip()


class IdleUnloader:
    """Wraps a backend so the model is loaded on first use and freed after an idle
    period. Transcription is already queued one-at-a-time by the worker, so the only
    cost of unloading is the reload delay on the next recording after a quiet spell.
    """

    def __init__(self, factory, name, idle_seconds):
        self._factory = factory
        self.name = name
        self.idle_seconds = idle_seconds
        self._model = None
        self._last_used = 0.0
        self._lock = threading.RLock()
        threading.Thread(target=self._watch, daemon=True).start()
        logger.info(f"[Ear] {name} will load on first use and unload after "
                    f"{idle_seconds // 60} min idle")

    def transcribe(self, path: Path) -> str:
        with self._lock:
            if self._model is None:
                t0 = time.time()
                self._model = self._factory()
                logger.info(f"[Ear] Loaded in {time.time() - t0:.1f}s")
            self._last_used = time.time()
            try:
                return self._model.transcribe(path)
            finally:
                self._last_used = time.time()  # don't unload mid-batch on a long file

    def _watch(self):
        while True:
            time.sleep(30)
            with self._lock:
                if self._model is None:
                    continue
                idle = time.time() - self._last_used
                if idle < self.idle_seconds:
                    continue
                logger.info(f"[Ear] Idle {idle / 60:.0f} min, unloading {self.name}")
                self._model = None
                gc.collect()
                try:
                    import torch
                    torch.cuda.empty_cache()
                except ImportError:
                    pass


BACKENDS = {
    "faster-whisper": FasterWhisperTranscriber,
    "whisper": FasterWhisperTranscriber,
    "parakeet": ParakeetTranscriber,
    "canary": CanaryQwenTranscriber,
}


def load_transcriber(name: str = None, idle_unload_minutes: float = None):
    """idle_unload_minutes: 0 keeps the model resident; None reads
    NOTEINATOR_IDLE_UNLOAD_MINUTES (default 5)."""
    name = (name or os.getenv("NOTEINATOR_ASR", "parakeet")).lower()
    if name not in BACKENDS:
        raise ValueError(f"Unknown ASR backend '{name}'. Options: {sorted(set(BACKENDS))}")
    if idle_unload_minutes is None:
        idle_unload_minutes = float(os.getenv("NOTEINATOR_IDLE_UNLOAD_MINUTES", "5"))
    if idle_unload_minutes > 0:
        return IdleUnloader(BACKENDS[name], name, int(idle_unload_minutes * 60))
    return BACKENDS[name]()
