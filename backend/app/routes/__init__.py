"""API routers for every studio feature.

``common.api_router`` is the single router the app mounts; each feature module
owns its own sub-router and is included here, so adding a feature is one import.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import clip_maker, editor, notes, summarizer, video_to_text
from .common import api_router

for _module in (clip_maker, editor, summarizer, video_to_text, notes):
    api_router.include_router(_module.router)

__all__ = ["api_router", "clip_maker", "editor", "summarizer", "video_to_text", "notes"]
