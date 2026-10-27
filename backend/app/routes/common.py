"""Shared plumbing for the feature routers.

Every feature follows the same shape: accept input -> create a Task -> run the
pipeline on a worker thread -> let the frontend poll ``/api/<feature>/status``.
These helpers keep that loop identical in each router.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile

from ..config import MEDIA_DIR, MEDIA_MOUNT, UPLOAD_DIR
from ..tasks import STORE, Task
from ..engine import ingest

# feature routers are merged into ``api_router`` by ``routes/__init__.py``
api_router = APIRouter()

JsonDict = Dict[str, Any]
Worker = Callable[[Task], JsonDict]


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
def start_task(feature: str, steps: List[str], worker: Worker) -> JsonDict:
    """Register a task and launch *worker* on a daemon thread."""
    task = STORE.create(feature, steps)
    STORE.run(task, lambda: worker(task), STORE)
    return {"task_id": task.id, "status": "queued"}


def status_of(task_id: str) -> JsonDict:
    task = STORE.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"unknown task {task_id}")
    return task.public()


def wait_for(task: Task, index: int, progress: float, stage: str) -> None:
    STORE.stage(task.id, index, progress, stage)


# --------------------------------------------------------------------------
# Media helpers
# --------------------------------------------------------------------------
def media_url(path: Path | str) -> str:
    """Absolute site path for a file that lives under ``MEDIA_DIR``."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(Path(MEDIA_DIR).resolve())
    except ValueError:
        return str(p)
    return f"/{MEDIA_MOUNT}/{rel.as_posix()}"


async def accept_upload(file: UploadFile, *, subdir: str = "uploads") -> Path:
    """Stream an uploaded video to disk (chunked, never fully in RAM)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    dest_dir = Path(UPLOAD_DIR) / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = os.path.basename(file.filename).replace(" ", "_")
    stamp = int(time.time() * 1000)
    path = dest_dir / f"{stamp}_{safe}"

    with open(path, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    return path


def require_source(source: Optional[str]) -> str:
    source = (source or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="`source` is required (URL or file path)")
    return source


def resolve_media(ref: str) -> str:
    """Turn a frontend reference into a local path.

    Accepts ``/media/<task>/clip_01.mp4`` (returned by :func:`media_url`), an
    absolute local path, or a remote URL (downloaded via yt-dlp).
    """
    ref = (ref or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="`source` is required")
    if ref.startswith(f"/{MEDIA_MOUNT}/"):
        rel = ref[len(MEDIA_MOUNT) + 2:]   # drop the leading "/media/"
        path = (Path(MEDIA_DIR) / rel).resolve()
        if not str(path).startswith(str(Path(MEDIA_DIR).resolve())):
            raise HTTPException(status_code=400, detail="invalid media path")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"media not found: {ref}")
        return str(path)
    if ref.startswith(("http://", "https://")):
        path, _ = ingest.resolve_input(ref)
        return path
    if not Path(ref).is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {ref}")
    return ref


def transcribe_path(path: Path | str, *, model_size: Optional[str] = None,
                    language: Optional[str] = None) -> Dict[str, Any]:
    """Audio -> word-level transcript for any video/audio file."""
    audio = ingest.extract_audio(str(path))
    try:
        return ingest.run_transcribe(audio, model_size=model_size, language=language)
    finally:
        # the temp wav is only needed for this call
        try:
            os.unlink(audio)
        except OSError:
            pass


def publish(src: Path | str, task_id: str, name: Optional[str] = None) -> str:
    """Make *src* playable over HTTP for this task's result.

    Hard-links when possible (same filesystem, no copy), otherwise copies.
    Returns the ``/media/...`` URL, which is what the frontend player loads.
    """
    src = Path(src)
    if not src.is_file():
        return ""
    out_dir = Path(MEDIA_DIR) / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (name or src.name)
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError:
            import shutil

            shutil.copy2(src, dst)
    return media_url(dst)
