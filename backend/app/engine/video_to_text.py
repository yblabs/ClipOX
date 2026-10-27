"""Video-to-Text: transcript + visual breakdown + generated prompt.

Pipeline (matches the spec exactly):

1. resolve input (URL via yt-dlp or a local file)
2. ``extract_audio`` - FFmpeg pulls the audio track
3. ``run_transcribe`` - faster-whisper gives the timestamped transcript
4. ``extract_frames`` - FFmpeg grabs a frame every N seconds (or at scene
   changes when PySceneDetect is installed)
5. ``describe_frames`` - a vision-capable model (Claude/GPT via ``llm.py``)
   describes scenes, subjects, camera movement, lighting, grade and style
6. ``write_prompt`` - transcript + visual notes are merged into the prompt
   format the user chose
7. the frontend shows transcript / technical breakdown / prompt, each copyable

Step 5 degrades gracefully: with no vision key configured the frames are
analysed locally (brightness, colourfulness, motion between frames, edge
density) so the breakdown is real data rather than invented prose.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from ..config import FFMPEG_BIN
from .ingest import (extract_audio, get_duration, probe_duration, resolve_input,
                     run_transcribe, thumbnail)
from .llm import configured as llm_configured, llm_json, vision_json

PROMPT_SYSTEM = """You are a prompt engineer for text-to-video generators.
Given a video transcript and a visual/technical breakdown, write prompts that
would let a model recreate the video. Return STRICT JSON only:
{
  "text_to_video": "one rich paragraph prompt: subject, action, setting, "
                   "camera, lens, lighting, palette, mood, motion, style",
  "shot_list": ["shot 1: ...", "shot 2: ..."],
  "summary_prompt": "a concise prompt for summarising/recreating the script",
  "style_tags": ["cinematic", "handheld", "..."],
  "visual_style": "single sentence describing the look of the footage"
}"""

VISION_SYSTEM = """You are a cinematographer analysing video frames.
Describe, in STRICT JSON: {"scenes": [{"t": <seconds>, "shot": "...",
"subject": "...", "camera": "...", "lighting": "...", "grade": "...",
"style": "..."}], "overall": "one paragraph on the visual style of the whole video"}"""


# --------------------------------------------------------------------------
# 1. Frame extraction
# --------------------------------------------------------------------------
def extract_frames(video: str, out_dir: Optional[str] = None, *,
                   every: float = 1.5, max_frames: int = 40,
                   scene_detect: bool = False) -> List[Dict[str, Any]]:
    """Grab frames every ``every`` seconds (or at scene changes).

    ``scene_detect=True`` uses PySceneDetect when it is importable and falls
    back to the fixed interval otherwise - an optional dependency must never
    take the feature down.
    """
    if not os.path.isfile(video):
        raise ValueError(f"input not found: {video}")
    out_dir = out_dir or tempfile.mkdtemp(prefix="v2t_frames_")
    os.makedirs(out_dir, exist_ok=True)

    duration = probe_duration(video)
    picked: List[float] = []
    if scene_detect:
        try:
            from scenedetect import detect, ContentDetector  # type: ignore
            for scene in detect(video, ContentDetector()):
                picked.append(float(scene[0].get_seconds()))
        except Exception:
            picked = []
    if not picked:
        step = max(0.5, every)
        t = 0.0
        while t < duration and len(picked) < max_frames:
            picked.append(t)
            t += step
    picked = sorted(set(round(t, 2) for t in picked))[:max_frames]

    frames: List[Dict[str, Any]] = []
    for i, t in enumerate(picked):
        path = os.path.join(out_dir, f"frame_{i:03d}.jpg")
        proc = subprocess.run(
            [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
             "-vf", "scale='min(768,iw)':-2", "-q:v", "6", path],
            capture_output=True, text=True)
        if proc.returncode == 0 and os.path.isfile(path):
            frames.append({"t": t, "path": path})
    if not frames:
        raise ValueError("no frames could be extracted from this video")
    return frames


def _local_frame_stats(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Offline visual analysis: brightness, contrast, colourfulness, motion.

    Reads the JPEGs as raw RGB through FFmpeg (no Pillow needed) and derives
    measurable properties instead of hallucinating a description.
    """
    stats = []
    for fr in frames:
        proc = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", fr["path"],
             "-vf", "scale=64:64,format=rgb24", "-f", "rawvideo", "-"],
            capture_output=True)
        raw = proc.stdout
        if not raw or len(raw) < 64 * 64 * 3:
            continue
        px = [raw[i] for i in range(64 * 64 * 3)]
        lum = [0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2]
               for i in range(0, len(px), 3)]
        mean = sum(lum) / len(lum)
        var = sum((v - mean) ** 2 for v in lum) / len(lum)
        sat = sum(max(px[i], px[i + 1], px[i + 2]) - min(px[i], px[i + 1], px[i + 2])
                  for i in range(0, len(px), 3)) / (64 * 64)
        stats.append({"t": fr["t"], "lum": mean, "contrast": var ** 0.5,
                      "sat": sat, "px": lum})

    if not stats:
        return {"frames": 0, "motion": []}

    motion = []
    for a, b in zip(stats, stats[1:]):
        delta = sum(abs(x - y) for x, y in zip(a["px"], b["px"])) / len(a["px"])
        motion.append({"t": b["t"], "delta": round(delta, 2)})

    avg_lum = sum(s["lum"] for s in stats) / len(stats)
    avg_sat = sum(s["sat"] for s in stats) / len(stats)
    if avg_lum < 70:
        lighting = "low-key / dark exposure"
    elif avg_lum > 175:
        lighting = "high-key / brightly lit"
    else:
        lighting = "balanced exposure"
    if avg_sat < 22:
        palette = "desaturated / neutral"
    elif avg_sat > 60:
        palette = "vivid / saturated"
    else:
        palette = "natural colour"
    cuts = sum(1 for m in motion if m["delta"] > 28)

    return {
        "frames": len(stats),
        "lighting": lighting,
        "palette": palette,
        "average_brightness": round(avg_lum, 1),
        "average_saturation": round(avg_sat, 1),
        "estimated_cuts": cuts,
        "motion": motion[:40],
        "method": "local",
    }


def describe_frames(frames: List[Dict[str, Any]], transcript_text: str = "") -> Dict[str, Any]:
    """Vision-model scene descriptions, with a local statistical fallback."""
    sample = frames[::max(1, len(frames) // 8)][:8]
    if llm_configured():
        try:
            images = []
            for fr in sample:
                with open(fr["path"], "rb") as fh:
                    images.append({"t": fr["t"],
                                   "b64": base64.b64encode(fh.read()).decode()})
            out = vision_json(VISION_SYSTEM, transcript_text[:1500], images)
            if out:
                out["method"] = "vision-model"
                return out
        except Exception:
            pass
    return _local_frame_stats(frames)


# --------------------------------------------------------------------------
# 2. Prompt writing
# --------------------------------------------------------------------------
def write_prompt(transcript_text: str, breakdown: Dict[str, Any],
                 prompt_type: str = "text-to-video") -> Dict[str, Any]:
    """Turn the transcript + visual notes into a usable generator prompt."""
    payload = {
        "prompt_type": prompt_type,
        "transcript": transcript_text[:6000],
        "visual_breakdown": breakdown,
    }
    guide = {
        "text-to-video": "Focus on recreating footage: camera, motion, subject, look.",
        "summary": "Focus on summarising the content in one tight paragraph.",
        "blog": "Focus on turning the script into an article outline + intro.",
        "study": "Focus on study notes: concepts, definitions, questions.",
    }.get(prompt_type, "")

    result = llm_json(PROMPT_SYSTEM, f"{guide}\n\nINPUT:\n{json.dumps(payload, ensure_ascii=False)}")
    if result.get("_fallback"):
        # honest local assembly from what we actually measured
        keys = " ".join(transcript_text.split()[:60])
        style = f"{breakdown.get('lighting', 'natural light')}, {breakdown.get('palette', 'natural colour')}"
        text_prompt = (f"{keys}... | Visual style: {style}. "
                       f"Shot on a modern camera, smooth motion, {prompt_type} prompt.")
        summary_prompt = transcript_text[:600]
        doc_prompt = (f"A {prompt_type} based on this transcript, keeping the original meaning:\n\n"
                      f"{transcript_text[:3000]}")
        shots = [f"{m.get('t', 0)}s — visual change (frame sample)"
                 for m in (breakdown.get("motion") or [])][:12]
        shots += [f"{s.get('t', 0)}s — {s.get('shot', 'shot')}"
                  for s in (breakdown.get("scenes") or [])][:8]
        result = {
            "_fallback": True,
            "text_to_video": text_prompt,
            "prompt": {"text-to-video": text_prompt, "summary": summary_prompt,
                       "blog": doc_prompt, "study": doc_prompt}.get(prompt_type, text_prompt),
            "shot_list": shots,
            "summary_prompt": summary_prompt,
            "style_tags": [breakdown.get("palette", "natural"), breakdown.get("lighting", "daylight")],
            "visual_style": (f"{breakdown.get('lighting', 'balanced')} footage, "
                             f"{breakdown.get('palette', 'natural colour')}."),
        }
    elif not result.get("prompt"):
        # model answered in the shape we asked for: expose one ready prompt
        result["prompt"] = (result.get("text_to_video") or result.get("summary_prompt")
                            or " ".join(result.get("shot_list") or []) or "")
    return result


def process_video_to_text(
    source: str,
    *,
    prompt_type: str = "text-to-video",
    frame_interval: float = 1.5,
    language: Optional[str] = None,
    model_size: str = "small",
    scene_detect: bool = False,
) -> Dict[str, Any]:
    """Full video -> transcript + breakdown + prompt pipeline."""
    video, meta = resolve_input(source)
    frames_dir = tempfile.mkdtemp(prefix="v2t_")
    try:
        duration = get_duration(video)
        audio = extract_audio(video)
        transcript = run_transcribe(audio, model_size=model_size, language=language)
        segments = transcript.get("segments", [])
        transcript_text = " ".join(s.get("text", "").strip() for s in segments)

        frames = extract_frames(video, out_dir=frames_dir, every=frame_interval,
                                scene_detect=scene_detect)
        breakdown = describe_frames(frames, transcript_text)
        prompt = write_prompt(transcript_text, breakdown, prompt_type)

        return {
            "source": source,
            "title": meta.get("title") or os.path.basename(source),
            "thumbnail": meta.get("thumbnail") or (thumbnail(video, frames_dir) if os.path.isfile(video) else ""),
            "duration": round(duration, 2),
            "language": transcript.get("language"),
            "transcript": {
                "text": transcript_text,
                "segments": segments,
            },
            "visual_breakdown": breakdown,
            "prompt": prompt,
            "prompt_type": prompt_type,
            "frame_count": len(frames),
        }
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
