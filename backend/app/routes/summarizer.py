"""AI Video Summarizer routes.

``POST /api/summarizer/process``  input + options -> background job
``GET  /api/summarizer/status/{id}``  progress, then the structured summary
``POST /api/summarizer/export``   Markdown / PDF / SRT / VTT / TXT download

The result carries ``video_url`` so the summary panel can sit next to a seekable
player, and ``summary.chapters[*].start`` values are plain seconds - clicking a
timestamp is just ``player.currentTime = start``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import MEDIA_DIR, WHISPER_MODEL
from ..engine import summarizer
from ..engine.ingest import resolve_input, thumbnail
from ..tasks import STORE
from .common import media_url, publish, start_task, status_of, transcribe_path

router = APIRouter(prefix="/api/summarizer", tags=["summarizer"])

STEPS = [
    "Downloading video",
    "Extracting audio",
    "Transcribing with Whisper",
    "Generating AI summary",
    "Packaging result",
]

LENGTHS = ("brief", "detailed", "bullets")
FORMATS = ("chapters", "actions", "moments")
EXPORTS = ("md", "pdf", "srt", "vtt", "txt")


class SummaryOptions(BaseModel):
    summary_length: str = Field("detailed", description="brief | detailed | bullets")
    formats: Dict[str, bool] = Field(default_factory=lambda: {
        "chapters": True, "actions": True, "moments": True,
    })
    language: Optional[str] = None
    model_size: str = WHISPER_MODEL


class ProcessRequest(BaseModel):
    source: str = Field(..., description="video URL or local file path")
    options: SummaryOptions = Field(default_factory=SummaryOptions)


class ExportRequest(BaseModel):
    task_id: str
    format: str = Field("md", description="md | pdf | srt | vtt | txt")


@router.post("/process")
def process(body: ProcessRequest) -> Dict[str, Any]:
    """Start summarising; poll ``/status/{task_id}`` for the structured JSON."""
    source = body.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="`source` is required")
    opts = body.options
    length = opts.summary_length if opts.summary_length in LENGTHS else "detailed"
    formats = {k: bool(opts.formats.get(k, False)) for k in FORMATS}

    def worker(t):
        STORE.stage(t.id, 0, 4, STEPS[0])
        video, meta = resolve_input(source)
        STORE.stage(t.id, 1, 12, STEPS[1])
        video_url = publish(video, t.id)
        poster = thumbnail(video, str(Path(MEDIA_DIR) / t.id))
        STORE.stage(t.id, 2, 22, STEPS[2])
        transcript = transcribe_path(video, model_size=opts.model_size, language=opts.language)
        STORE.stage(t.id, 3, 70, STEPS[3])
        summary = summarizer.summarize_transcript(
            transcript,
            duration=float(meta.get("duration") or transcript.get("duration") or 0.0),
            summary_length=length,
            formats=formats,
            title_hint=meta.get("title", ""),
        )
        STORE.stage(t.id, 4, 94, STEPS[4])
        return {
            "task_id": t.id,
            "source": source,
            "title": meta.get("title") or Path(video).stem,
            "thumbnail": meta.get("thumbnail") or (media_url(poster) if poster else ""),
            "video_url": video_url,
            "duration": summary.get("duration") or meta.get("duration"),
            "language": transcript.get("language"),
            "summary": summary,
            "transcript": {
                "text": transcript.get("text", ""),
                "segments": transcript.get("segments", []),
            },
            "llm": "off" if summary.get("_fallback") else "on",
        }

    return start_task("summarizer", STEPS, worker)


@router.get("/status/{task_id}")
def status(task_id: str) -> Dict[str, Any]:
    return status_of(task_id)


@router.post("/export")
def export(body: ExportRequest) -> FileResponse:
    """Render the finished summary/transcript as a downloadable file."""
    task = STORE.get(body.task_id)
    if task is None or task.status != "done" or not task.result:
        raise HTTPException(status_code=409, detail="task is not finished")
    fmt = body.format.lower()
    if fmt not in EXPORTS:
        raise HTTPException(status_code=400, detail=f"format must be one of {EXPORTS}")

    result = task.result
    out_dir = Path(MEDIA_DIR) / task.id
    out_dir.mkdir(parents=True, exist_ok=True)
    segments = (result.get("transcript") or {}).get("segments", [])
    summary = result.get("summary") or {}
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in result.get("title", "summary"))[:40]
    base = base or "summary"

    if fmt in ("md", "txt"):
        path = out_dir / f"{base}.md"
        md = summarizer.summary_to_markdown(
            summary, (result.get("transcript") or {}).get("text", "")
        )
        path.write_text(md, encoding="utf-8")
        if fmt == "txt":
            plain = out_dir / f"{base}.txt"
            plain.write_text(md, encoding="utf-8")
            path = plain
    elif fmt == "srt":
        path = out_dir / f"{base}.srt"
        path.write_text(summarizer.transcript_to_srt(segments), encoding="utf-8")
    elif fmt == "vtt":
        path = out_dir / f"{base}.vtt"
        path.write_text(summarizer.transcript_to_vtt(segments), encoding="utf-8")
    else:  # pdf
        md = summarizer.summary_to_markdown(
            summary, (result.get("transcript") or {}).get("text", "")
        )
        path = Path(summarizer.text_to_pdf(md, str(out_dir / f"{base}.pdf"),
                                           title=result.get("title", "Summary")))

    media = "text/plain" if path.suffix in (".md", ".txt", ".srt", ".vtt") else "application/pdf"
    return FileResponse(path, media_type=media, filename=Path(path).name)
