"""Video-to-Text routes: transcript + visual breakdown + generated prompt.

``POST /api/video-to-text/process``   start the pipeline
``GET  /api/video-to-text/status/{id}``  poll, then transcript/breakdown/prompt
``POST /api/video-to-text/prompt``    re-run only the prompt step with another type
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import MEDIA_DIR, WHISPER_MODEL
from ..engine import video_to_text
from ..tasks import STORE
from .common import media_url, publish, resolve_media, start_task, status_of, transcribe_path

router = APIRouter(prefix="/api/video-to-text", tags=["video-to-text"])

STEPS = [
    "Preparing video",
    "Extracting audio",
    "Transcribing with Whisper",
    "Extracting frames",
    "Analysing visuals",
    "Writing the prompt",
]

PROMPT_TYPES = ("text-to-video", "summary", "blog", "study")


class ProcessRequest(BaseModel):
    source: str = Field(..., description="video URL or local file path")
    prompt_type: str = Field("text-to-video", description="text-to-video | summary | blog | study")
    frame_interval: float = Field(1.5, ge=0.5, le=30, description="seconds between sampled frames")
    model_size: str = WHISPER_MODEL
    language: Optional[str] = None
    scene_detect: bool = Field(False, description="sample at scene changes (PySceneDetect) when installed")


class PromptRequest(BaseModel):
    task_id: str
    prompt_type: str = "text-to-video"


@router.post("/process")
def process(body: ProcessRequest) -> Dict[str, Any]:
    """Full pipeline: audio -> transcript -> frames -> visual notes -> prompt."""
    source = body.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="`source` is required")
    ptype = body.prompt_type if body.prompt_type in PROMPT_TYPES else "text-to-video"

    def worker(t):
        from ..engine.ingest import resolve_input, thumbnail

        STORE.stage(t.id, 0, 5, STEPS[0])
        video, meta = resolve_input(source)
        video_url = publish(video, t.id)
        poster = thumbnail(video, str(Path(MEDIA_DIR) / t.id))
        STORE.stage(t.id, 2, 25, STEPS[2])
        transcript = transcribe_path(video, model_size=body.model_size, language=body.language)
        STORE.stage(t.id, 3, 55, STEPS[3])
        # frames stay on disk so the user can inspect which moments were analysed
        frames = video_to_text.extract_frames(
            video, str(Path(MEDIA_DIR) / t.id / "frames"),
            every=body.frame_interval, scene_detect=body.scene_detect,
        )
        STORE.stage(t.id, 4, 70, STEPS[4])
        transcript_text = " ".join(s.get("text", "") for s in transcript.get("segments", []))
        breakdown = video_to_text.describe_frames(frames, transcript_text)
        STORE.stage(t.id, 5, 85, STEPS[5])
        prompt = video_to_text.write_prompt(transcript_text, breakdown, ptype)

        return {
            "task_id": t.id,
            "source": source,
            "title": meta.get("title") or Path(video).stem,
            "thumbnail": meta.get("thumbnail") or (media_url(poster) if poster else ""),
            "video_url": video_url,
            "duration": transcript.get("duration") or meta.get("duration"),
            "language": transcript.get("language"),
            "prompt_type": ptype,
            "frame_count": len(frames),
            "transcript": {
                "text": transcript.get("text", ""),
                "segments": transcript.get("segments", []),
            },
            "visual_breakdown": breakdown,
            "prompt": prompt,
        }

    return start_task("video-to-text", STEPS, worker)


@router.get("/status/{task_id}")
def status(task_id: str) -> Dict[str, Any]:
    return status_of(task_id)


@router.post("/prompt")
def regenerate_prompt(body: PromptRequest) -> Dict[str, Any]:
    """Re-run just the prompt-writing step (cheap - no re-transcription)."""
    task = STORE.get(body.task_id)
    if task is None or task.status != "done" or not task.result:
        raise HTTPException(status_code=409, detail="task is not finished")
    if body.prompt_type not in PROMPT_TYPES:
        raise HTTPException(status_code=400, detail=f"prompt_type must be one of {PROMPT_TYPES}")

    result = task.result
    text = (result.get("transcript") or {}).get("text", "")
    prompt = video_to_text.write_prompt(
        text, result.get("visual_breakdown") or {}, body.prompt_type
    )
    result["prompt"] = prompt
    result["prompt_type"] = body.prompt_type
    return {"task_id": task.id, "prompt_type": body.prompt_type, "prompt": prompt}
