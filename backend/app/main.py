"""WayinVideo Studio — FastAPI application.

Layout::

    app/
      config.py        environment-driven settings (paths, models, LLM, CORS)
      tasks.py         in-memory background task store (poll /api/.../status)
      routes/          HTTP layer, one module per feature
      engine/          the media AI engine (ffmpeg, whisper, scenes, ass, llm)

Run::

    uvicorn app.main:app --host 127.0.0.1 --port 8000

The static frontend lives one level up (``wayin-site/``) and is mounted at ``/``
so a single origin serves both the UI and the API during development.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, CORS_ORIGINS, MEDIA_DIR, MEDIA_MOUNT
from .routes import api_router

SITE_DIR = BASE_DIR.parent

app = FastAPI(
    title="WayinVideo Studio API",
    description="Clip Maker, AI Video Editor, Summarizer, Video-to-Text and Note Taker.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()] or ["*"],
    allow_credentials=False,   # "*" origins cannot be combined with credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    """Liveness + capability report (what is installed, what is configured).

    Registered *before* the static mounts so the "/" mount cannot shadow it.
    """
    from .engine import transcribe
    from .engine.llm import configured as llm_configured, provider_label

    return {
        "status": "ok",
        "whisper": {
            "available": transcribe.available(),
            "model": os.getenv("WV_WHISPER_MODEL", "base"),
        },
        "llm": "on" if llm_configured() else "local-fallback",
        "llm_provider": provider_label(),
        "media_mount": f"/{MEDIA_MOUNT}",
    }


# rendered clips, posters, exports (absolute paths under MEDIA_DIR)
app.mount(f"/{MEDIA_MOUNT}", StaticFiles(directory=str(MEDIA_DIR)), name="media")

# the static frontend, if it sits next to the backend folder
if (SITE_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")
