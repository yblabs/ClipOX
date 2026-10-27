"""Note Taker routes: transcript -> notes, technical mode, prompt generator, exports.

``POST /api/notes/process``     start the job
``GET  /api/notes/status/{id}`` poll, then transcript + notes + technical block
``POST /api/notes/prompt``      generate a downstream prompt from the transcript
``POST /api/notes/export``      md | pdf | docx | txt | srt | vtt download
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import MEDIA_DIR, WHISPER_MODEL
from ..engine import notes as notes_engine
from ..tasks import STORE
from .common import media_url, publish, resolve_media, start_task, status_of, transcribe_path

router = APIRouter(prefix="/api/notes", tags=["notes"])

STEPS = [
    "Preparing recording",
    "Extracting audio",
    "Transcribing with Whisper",
    "Labeling speakers",
    "Building notes",
    "Technical extraction",
]

EXPORTS = ("md", "pdf", "docx", "txt", "srt", "vtt")
PROMPT_TASKS = tuple(notes_engine.PROMPT_TASKS)


class ProcessRequest(BaseModel):
    source: str = Field(..., description="video/audio URL or local file path")
    technical: bool = Field(True, description="extract code, commands, tools and steps")
    speakers: bool = Field(True, description="add speaker labels to the transcript")
    model_size: str = WHISPER_MODEL
    language: Optional[str] = None


class PromptRequest(BaseModel):
    task_id: str
    task: str = Field("blog", description="blog | study | thread | video | email")
    extra: str = ""


class ExportRequest(BaseModel):
    task_id: str
    format: str = Field("md", description="md | pdf | docx | txt | srt | vtt")


@router.post("/process")
def process(body: ProcessRequest) -> Dict[str, Any]:
    source = body.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="`source` is required")

    def worker(t):
        from ..engine.ingest import resolve_input, thumbnail

        STORE.stage(t.id, 0, 5, STEPS[0])
        video, meta = resolve_input(source)
        video_url = publish(video, t.id)
        poster = thumbnail(video, str(Path(MEDIA_DIR) / t.id))
        STORE.stage(t.id, 2, 20, STEPS[2])
        transcript = transcribe_path(video, model_size=body.model_size, language=body.language)
        audio = None
        try:
            from ..engine.ingest import extract_audio

            audio = extract_audio(video)
            if body.speakers:
                STORE.stage(t.id, 3, 55, STEPS[3])
                notes_engine.detect_speakers(audio, transcript)
        finally:
            if audio:
                Path(audio).unlink(missing_ok=True)

        transcript["text"] = " ".join(s.get("text", "").strip()
                                      for s in transcript.get("segments", []))
        STORE.stage(t.id, 4, 65, STEPS[4])
        payload = {
            "source": source,
            "title": meta.get("title") or Path(video).stem,
            "thumbnail": meta.get("thumbnail") or (media_url(poster) if poster else ""),
            "video_url": video_url,
            "duration": transcript.get("duration") or meta.get("duration"),
            "language": transcript.get("language"),
            "transcript": transcript,
            "notes": notes_engine.build_notes(transcript, meta.get("title", "")),
            "technical": None,
            "prompt": None,
        }
        if body.technical:
            STORE.stage(t.id, 5, 85, STEPS[5])
            payload["technical"] = notes_engine.extract_technical(transcript)
        payload["task_id"] = t.id
        return payload

    return start_task("notes", STEPS, worker)


@router.get("/status/{task_id}")
def status(task_id: str) -> Dict[str, Any]:
    return status_of(task_id)


@router.post("/prompt")
def prompt(body: PromptRequest) -> Dict[str, Any]:
    """Turn the finished transcript into a ready-to-use prompt for another task."""
    task = STORE.get(body.task_id)
    if task is None or task.status != "done" or not task.result:
        raise HTTPException(status_code=409, detail="task is not finished")
    if body.task not in PROMPT_TASKS:
        raise HTTPException(status_code=400, detail=f"task must be one of {PROMPT_TASKS}")
    result = notes_engine.generate_prompt(task.result["transcript"], body.task, body.extra)
    task.result["prompt"] = result
    return {"task_id": task.id, "prompt": result}


@router.post("/export")
def export(body: ExportRequest) -> FileResponse:
    task = STORE.get(body.task_id)
    if task is None or task.status != "done" or not task.result:
        raise HTTPException(status_code=409, detail="task is not finished")
    fmt = body.format.lower()
    if fmt not in EXPORTS:
        raise HTTPException(status_code=400, detail=f"format must be one of {EXPORTS}")

    path = notes_engine.export_notes(task.result, fmt, str(Path(MEDIA_DIR) / task.id))
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if fmt == "docx" else
             "application/pdf" if fmt == "pdf" else "text/plain")
    return FileResponse(path, media_type=media, filename=Path(path).name)
