"""Clip Maker routes.

``POST /api/clip-maker/process``   start a job (JSON body *or* multipart upload)
``GET  /api/clip-maker/status/{id}``  poll progress, steps and the finished clips
``GET  /api/clip-maker/style-options``  the valid subtitle style values

``mode`` selects the output: ``normal`` (default) writes titles/tags/descriptions,
``hook`` additionally writes 7 scroll-stopping hooks per clip.

The heavy lifting lives in :mod:`engine.clip_maker`; this module is only the
HTTP/task boundary plus URL mapping for the rendered files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..config import CLIP_COUNT, MEDIA_DIR
from ..engine import clip_maker
from ..engine.ass import FONTS, PALETTES, PLACEMENTS, SIZES
from ..tasks import STORE
from .common import media_url, start_task, status_of

router = APIRouter(prefix="/api/clip-maker", tags=["clip-maker"])

STEPS = [
    "Preparing video",
    "Extracting audio",
    "Transcribing with Whisper",
    "Detecting scenes",
    "Scoring viral hooks",
    "Rendering vertical clips",
    "Writing titles, tags & descriptions",
]

MOTIONS = ("stationary", "moving")
PLACEMENT_KEYS = tuple(PLACEMENTS)
SIZE_KEYS = tuple(SIZES)

# Output mode: `normal` = titles/tags/descriptions only,
# `hook` = the same plus 7 scroll-stopping hooks per clip.
MODES = ("normal", "hook")
LAST_STEP = {
    "normal": "Writing titles, tags & descriptions",
    "hook": "Writing titles, tags & hooks",
}


class StyleConfig(BaseModel):
    """Subtitle style chosen in the collapsible options drawer."""

    motion: str = Field("stationary", description="stationary | moving (word-by-word bounce)")
    palette: str = Field("classic", description="colour preset key")
    font: str = Field("Montserrat", description="font family installed on the render host")
    size: str = Field("medium", description="small | medium | large")
    placement: str = Field("bottom", description="bottom | center | top")


class ProcessRequest(BaseModel):
    source: str = Field(..., description="video URL (YouTube etc.) or an already-uploaded file path")
    style: StyleConfig = Field(default_factory=StyleConfig)
    count: int = Field(CLIP_COUNT, ge=1, le=20)
    mode: str = Field("normal", description="normal = metadata only, hook = adds 7 hooks per clip")


def clean_style(style: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only known keys/values so a bad payload can never break ffmpeg."""
    return {
        "motion": style.get("motion") if style.get("motion") in MOTIONS else "stationary",
        "palette": style.get("palette") if style.get("palette") in PALETTES else "classic",
        "font": style.get("font") if style.get("font") in FONTS else FONTS[0],
        "size": style.get("size") if style.get("size") in SIZE_KEYS else "medium",
        "placement": style.get("placement") if style.get("placement") in PLACEMENT_KEYS else "bottom",
    }


def clean_mode(mode: Optional[str]) -> str:
    return mode if mode in MODES else "normal"


@router.post("/process")
async def process_json(body: ProcessRequest) -> Dict[str, Any]:
    """Start a clip job from a pasted URL or a local/uploaded file path."""
    style = clean_style(body.style.model_dump())
    return _start(body.source.strip(), style, max(1, min(body.count, 20)),
                  clean_mode(body.mode), None)


@router.post("/upload")
async def process_upload(
    file: UploadFile = File(..., description=".mp4 / .mov long-form video"),
    style: str = Form("{}", description="JSON-encoded StyleConfig"),
    count: int = Form(CLIP_COUNT),
    mode: str = Form("normal", description="normal | hook"),
) -> Dict[str, Any]:
    """Same as ``/process`` but the video arrives as a multipart upload."""
    from .common import accept_upload

    path = await accept_upload(file, subdir="clips")
    try:
        style_obj = json.loads(style or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="`style` must be valid JSON")
    return _start(str(path), clean_style(style_obj), max(1, min(count, 20)),
                  clean_mode(mode), path)


def _start(source: str, style: Dict[str, Any], count: int, mode: str,
           uploaded: Optional[Path]) -> Dict[str, Any]:
    if not source:
        raise HTTPException(status_code=400, detail="`source` is required")

    steps = list(STEPS)
    steps[-1] = LAST_STEP[mode]

    def worker(t):
        def on_progress(step: int, pct: float, label: str) -> None:
            STORE.stage(t.id, min(step, len(steps) - 1), pct, label)

        result = clip_maker.process(
            t.id, source=source, style=style, count=count, mode=mode,
            uploaded=uploaded, on_progress=on_progress,
        )
        out_dir = Path(MEDIA_DIR) / t.id
        for clip in result["clips"]:
            clip["url"] = media_url(out_dir / clip["file"])
            clip["poster_url"] = media_url(out_dir / clip["poster"])
            clip["ass_url"] = media_url(out_dir / clip["ass"])
            clip["download_name"] = f"clip_{clip['index']:02d}.mp4"
        result["task_id"] = t.id
        return result

    return start_task("clip-maker", steps, worker)


@router.get("/status/{task_id}")
def status(task_id: str) -> Dict[str, Any]:
    """Poll a clip job: ``status``, ``progress``, ``steps`` and ``result``."""
    return status_of(task_id)


@router.get("/style-options")
def style_options() -> Dict[str, Any]:
    """Everything the subtitle drawer can send."""
    return {
        "motion": list(MOTIONS),
        "palettes": [{"key": k, **v} for k, v in PALETTES.items()],
        "fonts": list(FONTS),
        "sizes": [{"key": k, "px": v} for k, v in SIZES.items()],
        "placements": list(PLACEMENT_KEYS),
    }
