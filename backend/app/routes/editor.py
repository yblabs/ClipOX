"""AI Video Editor routes.

``POST /api/clips/edit-clip``      re-cut / re-burn a single clip (spec path)
``POST /api/editor/transcribe``    word-level transcript + editable line schema
``POST /api/editor/detect-silence`` dead-air report with cut timestamps
``POST /api/editor/render``        generic render: spans + optional captions

All of them are synchronous on purpose: a clip is a few tens of seconds long,
so the round trip is shorter than the polling overhead. The long clip-maker
job is the only background task.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import MEDIA_DIR, SILENCE_DB, SILENCE_MIN_SEC, WHISPER_MODEL
from ..engine import edit
from ..engine.ass import FONTS, PALETTES, PLACEMENTS, SIZES
from .common import media_url, resolve_media, transcribe_path

router = APIRouter(prefix="/api", tags=["editor"])

OUT_SUBDIR = "edited"


class EditClipRequest(BaseModel):
    source: str = Field(..., description="/media/... clip URL, local path or video URL")
    kept_text: Optional[str] = Field(None, description="edited transcript; kept words stay, the rest is cut")
    spans: Optional[List[List[float]]] = Field(None, description="explicit [[start, end], ...] to keep")
    caption_lines: Optional[List[Dict[str, Any]]] = Field(None, description="segments (with words) to re-burn")
    style: Optional[Dict[str, Any]] = Field(None, description="subtitle style (see /api/clip-maker/style-options)")
    remove_silence: bool = False
    silence_threshold_db: float = SILENCE_DB
    silence_min_duration: float = SILENCE_MIN_SEC
    model_size: str = WHISPER_MODEL
    filename: Optional[str] = Field(None, description="download filename")


class TranscribeRequest(BaseModel):
    source: str
    model_size: str = WHISPER_MODEL
    language: Optional[str] = None


class SilenceRequest(BaseModel):
    source: str
    threshold_db: float = SILENCE_DB
    min_duration: float = SILENCE_MIN_SEC


class RenderRequest(BaseModel):
    source: str
    spans: Optional[List[List[float]]] = None
    caption_lines: Optional[List[Dict[str, Any]]] = None
    style: Optional[Dict[str, Any]] = None
    speed: float = 1.0
    keep_audio_only: bool = False


def _out_path(prefix: str, suffix: str = ".mp4") -> Path:
    import time

    out_dir = Path(MEDIA_DIR) / OUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{prefix}_{int(time.time() * 1000)}{suffix}"


@router.post("/clips/edit-clip")
def edit_clip(body: EditClipRequest) -> Dict[str, Any]:
    """Single-clip post-processing: text-based trim, silence removal, caption re-burn."""
    src = resolve_media(body.source)

    if not (body.kept_text or body.spans or body.caption_lines or body.remove_silence):
        raise HTTPException(status_code=400,
                            detail="nothing to do: send kept_text, spans, captions or remove_silence")

    out = _out_path("edit")
    try:
        result = edit.edit_clip(
            src,
            str(out),
            kept_text=body.kept_text,
            spans=body.spans,
            caption_lines=body.caption_lines,
            style=body.style,
            remove_silence=body.remove_silence,
            silence_threshold_db=body.silence_threshold_db,
            silence_min_duration=body.silence_min_duration,
            model_size=body.model_size,
        )
    except edit.EditError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface ffmpeg failures to the UI
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    result["url"] = media_url(out)
    result["download_name"] = body.filename or "clip_edited.mp4"
    result["source"] = body.source
    if body.kept_text is not None:
        result["kept_text"] = body.kept_text
    return result


@router.post("/editor/transcribe")
def transcribe(body: TranscribeRequest) -> Dict[str, Any]:
    """Word-level transcript for the text-based editor.

    Returns both the raw segments and ``lines`` (grouped, with per-word
    ``[start, end]``) which is what the transcript panel renders.
    """
    src = resolve_media(body.source)
    try:
        data = transcribe_path(src, model_size=body.model_size, language=body.language)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc))
    data["lines"] = edit.transcript_schema(
        [w for seg in data["segments"] for w in seg.get("words", [])]
    )["lines"]
    data["source"] = body.source
    data["duration"] = data.get("duration") or edit.probe_duration(src)
    return data


@router.post("/editor/detect-silence")
def detect_silence(body: SilenceRequest) -> Dict[str, Any]:
    """Silent gaps (default < -30 dBFS for > 1 s) plus the complementary kept spans."""
    src = resolve_media(body.source)
    try:
        report = edit.detect_silence(
            src, threshold_db=body.threshold_db, min_duration=body.min_duration
        )
    except edit.EditError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "threshold_db": report.threshold_db,
        "min_duration": report.min_duration,
        "duration": round(report.duration, 3),
        "silences": [{"start": round(s.start, 3), "end": round(s.end, 3),
                      "duration": round(s.duration, 3)} for s in report.silenced],
        "kept": [[round(a, 3), round(b, 3)] for a, b in report.kept],
        "removed_seconds": round(report.saved, 3),
    }


@router.post("/editor/render")
def render(body: RenderRequest) -> Dict[str, Any]:
    """Cut + optionally burn captions in one ffmpeg pass (speed control too)."""
    src = resolve_media(body.source)
    out = _out_path("render")
    spans = None
    if body.spans:
        spans = [(float(s), float(e)) for s, e in body.spans if float(e) > float(s)]

    ass_file = None
    try:
        if body.caption_lines:
            from ..engine.ass import generate_ass

            ass_file = str(out) + ".ass"
            generate_ass(body.caption_lines, body.style or {}, ass_file)
        edit.render_final_video(
            src, str(out), spans=spans, ass_path=ass_file,
            speed=max(0.25, min(body.speed, 4.0)),
        )
    except edit.EditError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        if ass_file and os.path.isfile(ass_file):
            try:
                os.unlink(ass_file)
            except OSError:
                pass

    return {
        "url": media_url(out),
        "duration": round(edit.probe_duration(out), 3),
        "spans": [[round(s, 3), round(e, 3)] for s, e in (spans or [])],
        "download_name": "render.mp4",
    }


@router.get("/editor/options")
def options() -> Dict[str, Any]:
    """Style + silence options the editor panel can send."""
    return {
        "motion": ["stationary", "moving"],
        "palettes": [{"key": k, **v} for k, v in PALETTES.items()],
        "fonts": list(FONTS),
        "sizes": [{"key": k, "px": v} for k, v in SIZES.items()],
        "placements": list(PLACEMENTS),
        "silence": {"threshold_db": SILENCE_DB, "min_duration": SILENCE_MIN_SEC},
    }
