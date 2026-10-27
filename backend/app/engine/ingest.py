"""Input acquisition + shared helpers for the summarizer / notes / video-to-text.

Thin, honest wrappers around :mod:`media` and :mod:`transcribe` so every
feature starts from the same two calls:

``resolve_input(source) -> (local_path, meta)``  URL via yt-dlp, or a local file
``extract_audio`` / ``run_transcribe``            the Whisper input chain

Keeping them here means the features never shell out themselves and never
duplicate the yt-dlp options.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..config import DOWNLOAD_DIR, UPLOAD_DIR
from . import media
from .transcribe import transcribe_audio as _whisper


def resolve_input(source: str, *, workdir: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
    """Return ``(local_file_path, metadata)`` for a URL or a local path.

    URLs are downloaded once into ``DOWNLOAD_DIR`` and re-used on later runs;
    metadata comes from yt-dlp when available and ffprobe otherwise.
    """
    source = (source or "").strip()
    if not source:
        raise media.MediaError("no input provided")

    if source.startswith(("http://", "https://")):
        meta: Dict[str, Any] = {}
        try:
            meta = dict(media.fetch_metadata(source))
        except Exception:
            meta = {}
        path = media.download_url(source, workdir or DOWNLOAD_DIR)
        meta.setdefault("title", path.stem)
        meta.setdefault("thumbnail", "")
        if not meta.get("duration"):
            try:
                meta["duration"] = media.duration_of(path)
            except Exception:
                meta["duration"] = 0.0
        meta["local_path"] = str(path)
        return str(path), meta

    path = Path(os.path.expanduser(source))
    if not path.is_file():
        raise media.MediaError(f"file not found: {source}")
    meta = {"title": path.stem, "thumbnail": "", "webpage_url": "", "duration": 0.0}
    try:
        probe = media.video_meta(path)
        meta["duration"] = probe.get("duration", 0.0)
        meta["title"] = probe.get("title") or path.stem
    except Exception:
        pass
    meta["local_path"] = str(path)
    return str(path), meta


# -- aliases used by the feature modules -----------------------------------
def fetch_info(url: str) -> Dict[str, Any]:
    """yt-dlp metadata without downloading."""
    return media.fetch_metadata(url)


get_video_info = fetch_info


def local_duration(path: str) -> float:
    return media.duration_of(path)


get_duration = local_duration
probe_duration = local_duration


def extract_audio(src: str, dst: Optional[str] = None) -> str:
    """Mono 16 kHz WAV for Whisper; written to a temp file when *dst* is omitted."""
    if dst is None:
        dst = tempfile.mktemp(suffix=".wav", prefix="wv_audio_")
    return str(media.extract_audio(src, dst))


def thumbnail(src: str, out_dir: str) -> str:
    """Poster JPEG (~1 s in) inside *out_dir*; returns the path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dst = out / (Path(src).stem + "_thumb.jpg")
    try:
        return str(media.make_thumbnail(src, dst, at_sec=1.0))
    except Exception:
        return ""


def run_transcribe(
    audio_path: str,
    *,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
    word_timestamps: bool = True,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Word-level transcript (``w/s/e`` word schema shared by every feature)."""
    return _whisper(
        audio_path,
        model_size=model_size,
        language=language,
        word_timestamps=word_timestamps,
    )


def upload_path(filename: str, chunk) -> Path:
    """Stream a multipart upload into ``UPLOAD_DIR``."""
    return media.save_upload(filename, chunk, UPLOAD_DIR)


__all__ = [
    "resolve_input", "fetch_info", "get_video_info", "local_duration",
    "get_duration", "probe_duration", "extract_audio", "thumbnail",
    "run_transcribe", "upload_path",
]
