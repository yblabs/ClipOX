"""
Whisper transcription layer.

Wraps ``faster-whisper`` (CTranslate2) with:
  * lazy model loading (the model is only downloaded once),
  * automatic device / compute-type selection,
  * word-level timestamps (required for karaoke subtitles and text-based edit),
  * a stable JSON schema consumed by every other feature.

Schema
------
.. code-block:: json

    {
      "language": "en",
      "duration": 612.4,
      "text": "...",
      "segments": [
        {"id": 0, "start": 0.0, "end": 4.2, "text": "...",
         "words": [{"w": "Hello", "s": 0.0, "e": 0.32}]}
      ]
    }
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import (
    WHISPER_COMPUTE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
    detect_compute_type,
    detect_device,
)

_model = None
_model_key: Optional[str] = None
_model_lock = threading.Lock()


class TranscriptionError(RuntimeError):
    pass


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def load_model(model_size: Optional[str] = None):
    """Load (and cache) a ``WhisperModel`` instance."""
    global _model, _model_key

    model_size = model_size or WHISPER_MODEL
    device = detect_device()
    compute = WHISPER_COMPUTE or detect_compute_type(device)
    key = f"{model_size}:{device}:{compute}"

    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - env dependent
            raise TranscriptionError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc

        _model = WhisperModel(model_size, device=device, compute_type=compute, download_root=None)
        _model_key = key
        return _model


def transcribe_audio(
    audio_path: Path | str,
    *,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
    word_timestamps: bool = True,
    vad_filter: bool = True,
    on_progress=None,
) -> Dict[str, Any]:
    """Transcribe *audio_path* and return word/segment level timestamps."""
    model = load_model(model_size)
    lang = language or (WHISPER_LANGUAGE or None)

    t0 = time.time()
    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=lang,
            word_timestamps=word_timestamps,
            vad_filter=vad_filter,
            beam_size=5,
            condition_on_previous_text=False,
        )
    except Exception as exc:  # pragma: no cover - model runtime errors
        raise TranscriptionError(str(exc)) from exc

    segments: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    total = float(getattr(info, "duration", 0) or 0)

    for idx, seg in enumerate(segments_iter):
        entry: Dict[str, Any] = {
            "id": idx,
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": seg.text.strip(),
            "words": [],
        }
        for w in seg.words or []:
            item = {
                "w": w.word.strip(),
                "s": round(float(w.start), 3),
                "e": round(float(w.end), 3),
            }
            entry["words"].append(item)
            words.append({**item, "seg": idx})
        segments.append(entry)

        if on_progress and total > 0:
            on_progress(min(99.0, (seg.end / total) * 100.0))

    if not segments:
        raise TranscriptionError(
            "No speech detected in the audio track (empty transcript)."
        )

    return {
        "language": getattr(info, "language", "") or "",
        "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 3),
        "duration": round(total or segments[-1]["end"], 3),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "words": words,
        "elapsed_sec": round(time.time() - t0, 2),
        "device": detect_device(),
    }


def format_timestamp(sec: float, *, ms: bool = False) -> str:
    """``HH:MM:SS.mmm`` (or ``MM:SS`` when *ms* is False)."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    if ms:
        return f"{h:02d}:{m:02d}:{s:02d}.{int((sec - int(sec)) * 1000):03d}"
    return (f"{h:02d}:" if h else "") + f"{m:02d}:{s:02d}"
