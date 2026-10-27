"""AI Video Summarizer.

Pipeline: input -> metadata -> audio -> word-level transcript -> structured
LLM summary -> exports (Markdown / PDF / SRT / VTT).

The LLM layer comes from ``llm.py`` and always returns *something*: with an
API key configured it returns model JSON, otherwise a deterministic extractive
fallback builds the same shape from the transcript. That keeps the frontend
identical in both modes.

``llm_json`` returns ``{"_fallback": True, ...}`` for the local path, so the
frontend can label summaries as "offline draft" instead of pretending a model
wrote them.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .ingest import (extract_audio, fetch_info, get_video_info, local_duration,
                     probe_duration, resolve_input, run_transcribe, thumbnail)
from .llm import llm_json, llm_text

SUMMARY_SYSTEM = """You are an expert video analyst and editor.
You will receive a transcript of a video, split into timestamped segments.
Return STRICT JSON only - no markdown fences, no commentary - with this shape:
{
  "title": "a compelling title for the video",
  "overview": "2-3 sentence neutral overview",
  "summary_length": "short" | "medium" | "long",
  "chapters": [{"start": <seconds float>, "title": "...", "summary": "..."}],
  "key_points": ["...", "..."],
  "action_items": ["...", "..."],
  "best_moments": [{"start": <seconds float>, "label": "...", "why": "..."}],
  "keywords": ["...", "..."]
}
Rules:
- Every "start" MUST be an exact timestamp that appears in the transcript.
- Chapters: 3-8 of them, in order, each covering a contiguous range.
- key_points: the ideas worth remembering, phrased as full sentences.
- action_items: concrete things a viewer could do (empty list if none).
- best_moments: 3-8 moments that would work as clips, with a reason.
- Length of the prose must follow the requested summary length.
"""

LENGTH_GUIDE = {
    "brief": "Executive/brief: overview of 2 sentences, at most 5 key points, "
             "concise chapter summaries of one sentence each.",
    "detailed": "Detailed: overview of a paragraph, 8-12 key points, chapter "
                "summaries of 2-3 sentences each, plus action items.",
    "bullets": "Bulleted key takeaways: no prose overview beyond one sentence, "
               "prioritise dense bullet lists over paragraphs.",
}

FORMAT_GUIDE = {
    "chapters": "Include a thorough chapter-by-chapter breakdown with timestamps.",
    "actions": "Include an action items / key learnings list.",
    "moments": "Include highlight reel / best moments timestamps.",
}


def _segments_to_text(segments: List[Dict[str, Any]], cap: int = 400) -> str:
    lines = []
    for seg in segments[:cap]:
        lines.append(f"[{float(seg['start']):.1f} - {float(seg['end']):.1f}] {seg.get('text', '').strip()}")
    return "\n".join(lines)


def _clamp_chapters(chapters: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    out = []
    for ch in chapters or []:
        try:
            start = max(0.0, min(float(ch.get("start", 0.0)), max(0.0, duration - 1)))
        except (TypeError, ValueError):
            continue
        out.append({"start": round(start, 2),
                    "title": str(ch.get("title") or "Chapter"),
                    "summary": str(ch.get("summary") or "")})
    out.sort(key=lambda c: c["start"])
    return out


def _fallback_summary(transcript: Dict[str, Any], duration: float,
                      summary_length: str, formats: Dict[str, bool]) -> Dict[str, Any]:
    """Extractive summary used when no LLM key is configured."""
    segments = transcript.get("segments", [])
    text = " ".join(s.get("text", "").strip() for s in segments).strip()

    # rank sentences by term frequency - a cheap but honest proxy for salience
    stop = {"the", "and", "that", "this", "with", "for", "you", "your", "are",
            "was", "were", "have", "has", "but", "not", "all", "can", "its",
            "about", "just", "like", "what", "when", "then", "they", "them",
            "there", "here", "into", "from", "have", "will", "would", "could",
            "because", "been", "being", "our", "out", "one", "get", "got"}
    words = [w for w in re.findall(r"[a-zA-Z']+", text.lower()) if len(w) > 3 and w not in stop]
    freq: Dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored = []
    for s in sentences:
        toks = re.findall(r"[a-zA-Z']+", s.lower())
        if len(toks) < 5:
            continue
        score = sum(freq.get(t, 0) for t in toks) / (len(toks) ** 0.6)
        scored.append((score, s.strip()))
    scored.sort(key=lambda x: -x[0])

    want = {"brief": 5, "detailed": 10, "bullets": 8}.get(summary_length, 6)
    keep = {s for _, s in sorted(scored[:want], key=lambda x: x[1])}

    key_points = [s for _, s in sorted(scored[:want], key=lambda x: -x[0])][:want]

    # "action items" without a model = sentences that actually instruct
    ACTION_RE = re.compile(
        r"\b(always|never|make sure|remember to|you should|you need to|try to|"
        r"don't|avoid|start by|use the|make a point to)\b", re.I)
    action_items = [s.strip() for _, s in sorted(scored, key=lambda x: -x[0])
                    if ACTION_RE.search(s)][:6]

    chapters = []
    n_ch = max(3, min(8, int(duration // 180) or 3))
    step = duration / n_ch
    for i in range(n_ch):
        start = i * step
        chunk = [s for s in segments if float(s["start"]) >= start
                 and float(s["start"]) < start + step]
        snippet = " ".join(s.get("text", "").strip() for s in chunk[:4]).strip()
        chapters.append({"start": round(start, 2),
                         "title": (snippet[:60] + "…") if len(snippet) > 60 else (snippet or f"Part {i + 1}"),
                         "summary": snippet[:220]})

    moments = []
    for seg in segments:
        t = seg.get("text", "")
        if "?" in t or "!" in t or re.search(r"\b(never|best|secret|mistake|why|how)\b", t, re.I):
            moments.append({"start": round(float(seg["start"]), 2),
                            "label": t.strip()[:80],
                            "why": "high-signal moment detected from the transcript"})
        if len(moments) >= 6:
            break

    keywords = [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:12]]

    return {
        "_fallback": True,
        "title": (sentences[0][:90] if sentences else "Video summary"),
        "overview": " ".join(sentences[:2])[:400],
        "summary_length": summary_length,
        "chapters": chapters,
        "key_points": key_points,
        "action_items": action_items,
        "best_moments": moments,
        "keywords": keywords,
        "_raw_kept": keep and list(keep)[:want],
    }


def summarize_transcript(
    transcript: Dict[str, Any],
    *,
    duration: float,
    summary_length: str = "detailed",
    formats: Optional[Dict[str, bool]] = None,
    title_hint: str = "",
) -> Dict[str, Any]:
    """Produce the structured summary object consumed by the frontend."""
    formats = formats or {}
    segments = transcript.get("segments", [])
    if not segments:
        raise ValueError("empty transcript - nothing to summarize")

    payload = {
        "video_title": title_hint,
        "duration_seconds": round(duration, 2),
        "requested_length": summary_length,
        "requested_formats": formats,
        "transcript": _segments_to_text(segments),
    }
    guide = LENGTH_GUIDE.get(summary_length, LENGTH_GUIDE["detailed"])
    fmt = " ".join(v for k, v in FORMAT_GUIDE.items() if formats.get(k))
    user = f"{guide}\n{fmt}\n\nINPUT:\n{json.dumps(payload, ensure_ascii=False)}"

    result = llm_json(SUMMARY_SYSTEM, user)

    if result.get("_fallback"):
        result = _fallback_summary(transcript, duration, summary_length, formats)
    else:
        result["chapters"] = _clamp_chapters(result.get("chapters", []), duration)
        result.setdefault("key_points", [])
        result.setdefault("action_items", [])
        result.setdefault("best_moments", [])
        result.setdefault("keywords", [])
        result.setdefault("overview", "")
        result.setdefault("title", title_hint or "Video summary")

    # formats the user did not ask for are simply dropped
    if not formats.get("chapters"):
        result["chapters"] = result.get("chapters", [])[:3] if summary_length != "brief" else result.get("chapters", [])[:2]
    if not formats.get("actions"):
        result["action_items"] = []
    if not formats.get("moments"):
        result["best_moments"] = []

    result["duration"] = round(duration, 2)
    result["summary_length"] = summary_length
    result["formats"] = formats
    result["segment_count"] = len(segments)
    return result


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _fmt_ts(seconds: float, sep: str = ".") -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if sep == ",":
        ms = int((s - int(s)) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"
    ms = int((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{ms:03d}"


def summary_to_markdown(summary: Dict[str, Any], transcript_text: str = "") -> str:
    """Render the structured summary as clean Markdown."""
    out: List[str] = [f"# {summary.get('title', 'Video summary')}", ""]
    if summary.get("overview"):
        out += ["> " + str(summary["overview"]).replace("\n", "\n> "), ""]

    if summary.get("chapters"):
        out.append("## Chapters")
        for ch in summary["chapters"]:
            out.append(f"- **[{_fmt_ts(ch['start'])}]({ch['start']}s)** {ch['title']}"
                       + (f" — {ch['summary']}" if ch.get("summary") else ""))
        out.append("")

    if summary.get("key_points"):
        out.append("## Key points")
        out += [f"- {p}" for p in summary["key_points"]]
        out.append("")

    if summary.get("action_items"):
        out.append("## Action items")
        out += [f"- [ ] {a}" for a in summary["action_items"]]
        out.append("")

    if summary.get("best_moments"):
        out.append("## Best moments")
        for m in summary["best_moments"]:
            out.append(f"- **[{_fmt_ts(m['start'])}]({m['start']}s)** {m.get('label', '')}"
                       + (f" — {m['why']}" if m.get("why") else ""))
        out.append("")

    if summary.get("keywords"):
        out.append("## Keywords")
        out.append(" ".join(f"`{k}`" for k in summary["keywords"]))
        out.append("")

    if transcript_text:
        out += ["## Transcript", "", transcript_text]
    return "\n".join(out)


def transcript_to_srt(segments: List[Dict[str, Any]]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        blocks.append(f"{i}\n{_fmt_ts(seg['start'], ',')} --> {_fmt_ts(seg['end'], ',')}\n{text}\n")
    return "\n".join(blocks)


def transcript_to_vtt(segments: List[Dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def text_to_pdf(text: str, out_path: str, *, title: str = "Summary") -> str:
    """Write a paginated PDF with the stdlib only (no reportlab dependency).

    A tiny but valid PDF: Helvetica, 10pt, ~90 characters per line, 52 lines
    per page. Good enough for a readable export without a rendering library.
    """
    from .pdfkit import write_text_pdf
    return write_text_pdf(text, out_path, title=title)


__all__ = [
    "summarize_transcript", "summary_to_markdown", "transcript_to_srt",
    "transcript_to_vtt", "text_to_pdf", "_segments_to_text", "fetch_info",
    "get_video_info", "resolve_input", "local_duration", "extract_audio",
    "run_transcribe", "probe_duration", "thumbnail", "llm_text",
]
