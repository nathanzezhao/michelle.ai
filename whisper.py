"""Local speech-to-text for the email composer body only.

First call downloads WHISPER_MODEL (default base, a few hundred MB); later calls reuse it.
Do not import-time load — that would stall /chat while the model fetches.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import wave

try:
    from faster_whisper import WhisperModel as _WhisperModel
except ImportError:  # package missing: never invent a transcript
    _WhisperModel = None


MIN_AUDIO_SECONDS = 0.6
MIN_TRANSCRIPT_WORDS = 4
MIN_TRANSCRIPT_CHARS = 12
_JUNK_PHRASES = frozenset(
    {
        "",
        ".",
        "..",
        "...",
        "thank you",
        "thanks",
        "thank you very much",
    }
)

_lock = threading.Lock()
_model = None
_loading = False


class WhisperError(Exception):
    """Route maps error_code onto the JSON `error` field (HTTP 200)."""

    error_code = "whisper_missing"


class WhisperMissing(WhisperError):
    error_code = "whisper_missing"


class WhisperBusy(WhisperError):
    error_code = "busy"


class WhisperDownloading(WhisperError):
    error_code = "downloading"


def is_junk_transcript(text) -> bool:
    """Silence / Whisper hallucinations that must never reach the LLM."""
    if text is None:
        return True
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return True
    core = cleaned.lower().strip(".,!?;:\"'")
    if core in _JUNK_PHRASES or cleaned.lower() in _JUNK_PHRASES:
        return True
    if len(cleaned) < MIN_TRANSCRIPT_CHARS:
        return True
    if len(cleaned.split()) < MIN_TRANSCRIPT_WORDS:
        return True
    return False


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """PCM WAV duration from the header. Unreadable bytes count as 0."""
    if not wav_bytes:
        return 0.0
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
            if rate <= 0:
                return 0.0
            return frames / float(rate)
    except Exception:
        return 0.0


def transcribe_wav(wav_bytes: bytes) -> str:
    """Transcribe a WAV. Raises WhisperBusy / Downloading / Missing. Never invents text."""
    global _model, _loading

    if _WhisperModel is None:
        raise WhisperMissing("faster-whisper is not installed")

    acquired = _lock.acquire(blocking=False)
    if not acquired:
        if _loading:
            raise WhisperDownloading("Whisper model is still downloading")
        raise WhisperBusy("Whisper is already transcribing")

    path = None
    try:
        if _model is None:
            _loading = True
            try:
                size = os.getenv("WHISPER_MODEL", "base") or "base"
                # CPU (this Mac) has no efficient float16 — pin compute_type
                # so CTranslate2 does not warn and fall back on every load.
                device = os.getenv("WHISPER_DEVICE", "cpu") or "cpu"
                compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8") or "int8"
                _model = _WhisperModel(size, device=device, compute_type=compute)
            except Exception as e:
                _model = None
                raise WhisperMissing(f"Whisper model failed to load: {e}") from e
            finally:
                _loading = False

        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(fd, wav_bytes)
        finally:
            os.close(fd)

        try:
            segments, _info = _model.transcribe(path)
            return "".join(seg.text for seg in segments).strip()
        except WhisperError:
            raise
        except Exception as e:
            raise WhisperMissing(f"Whisper transcribe failed: {e}") from e
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        _lock.release()
