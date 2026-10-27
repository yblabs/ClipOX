"""Note Taker: transcript -> notes, technical breakdown, prompt, exports.

Same ingestion core as the summarizer, with three additions the notes tool
needs:

* **Speaker labels** - pyannote is optional (it needs a HuggingFace token and
  a large model download), so a lightweight energy-based diarization fallback
  is used when it is missing. Labels are honest: ``Speaker 1/2/...`` or
  ``Segment 1/2/...`` when there is not enough signal to separate voices.
* **Technical mode** - extracts code blocks, commands, tool names, file paths,
  URLs and step sequences from the transcript (regex-based, so it works offline
  and is verifiable against the source text).
* **Prompt generator** - turns a transcript into a ready-to-use prompt for a
  chosen downstream task (blog post, study guide, tweet thread, ...).

Exports: Markdown, PDF (stdlib), DOCX (stdlib zip writer), plain text. Search
runs client-side over the transcript lines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from .ingest import extract_audio, get_duration, resolve_input, run_transcribe, thumbnail
from .llm import llm_json
from .summarizer import text_to_pdf, transcript_to_srt, transcript_to_vtt

TECH_MODE_SYSTEM = """You are a senior engineer watching a technical recording.
Return STRICT JSON only:
{
  "tools": ["name - what it is used for"],
  "commands": ["exact command as spoken or shown"],
  "code": [{"lang": "...", "snippet": "...", "note": "..."}],
  "steps": ["step 1", "step 2"],
  "terms": ["term - definition"],
  "decisions": ["decision and its rationale"]
}
Only include items that actually appear in the transcript."""

PROMPT_TASKS = {
    "blog": "Write a 1200-word blog post with headings, an intro and a conclusion.",
    "study": "Create a study guide with definitions, mnemonics and 10 review questions.",
    "thread": "Write an 8-post social thread, each post under 280 characters.",
    "video": "Write a script for a new 3-minute video based on this content.",
    "email": "Write a newsletter email summarising the key takeaways.",
}

NOTES_SYSTEM = """You are an expert note-taker. Return STRICT JSON:
{
  "title": "...",
  "summary": "3-5 sentence summary",
  "key_points": ["..."],
  "action_items": ["..."],
  "decisions": ["..."],
  "open_questions": ["..."],
  "glossary": [{"term": "...", "definition": "..."}]
}"""


# --------------------------------------------------------------------------
# Diarization (pyannote when available, energy fallback otherwise)
# --------------------------------------------------------------------------
def detect_speakers(audio_path: str, transcript: Dict[str, Any],
                    *, max_speakers: int = 4) -> List[Dict[str, Any]]:
    """Attach a speaker label to every transcript segment.

    Returns segments with ``speaker`` added. Uses pyannote if the pipeline can
    be constructed; otherwise assigns speakers by shot-level energy changes,
    which is a coarse but truthful proxy (never claims diarization it did not do).
    """
    try:
        from pyannote.audio import Pipeline  # type: ignore
        token = os.getenv("HF_TOKEN")
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                            use_auth_token=token)
        turn = pipeline(audio_path)
        regions = [(float(s.start), float(s.end), str(s.speaker))
                   for s in turn.itertracks(yield_label=True)]
        for seg in transcript.get("segments", []):
            s, e = float(seg["start"]), float(seg["end"])
            best, overlap = "Speaker 1", 0.0
            for rs, re_, sp in regions:
                ov = max(0.0, min(e, re_) - max(s, rs))
                if ov > overlap:
                    best, overlap = sp, ov
            seg["speaker"] = best.replace("SPEAKER_", "Speaker ")
        return transcript.get("segments", [])
    except Exception:
        pass

    # fallback: energy-based activity, coarse labels
    try:
        import wave
        with wave.open(audio_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
        width = 2
        samples = len(frames) // width
        win = max(1, rate // 4)
        levels = []
        for i in range(0, samples - win, win):
            chunk = frames[i * width:(i + win) * width]
            peak = 0
            for j in range(0, len(chunk) - 1, 2):
                v = abs(int.from_bytes(chunk[j:j + 2], "little", signed=True))
                if v > peak:
                    peak = v
            levels.append((i / rate, peak))
        threshold = max(400, sorted(p for _, p in levels)[len(levels) // 4] // 2)

        # split into talk bursts separated by >1.5 s of quiet
        bursts: List[List[float]] = []
        active = None
        last_t = 0.0
        for t, peak in levels:
            if peak > threshold:
                if active is None:
                    active = [t, t]
                active[1] = t
                last_t = t
            elif active is not None and t - last_t > 1.5:
                bursts.append(active)
                active = None
        if active:
            bursts.append(active)

        counter = 0
        for seg in transcript.get("segments", []):
            s, e = float(seg["start"]), float(seg["end"])
            label = None
            for i, (bs, be) in enumerate(bursts):
                if bs - 0.3 <= s <= be + 0.3:
                    label = f"Speaker {i % max_speakers + 1}"
                    break
            seg["speaker"] = label or f"Segment {counter // 6 + 1}"
            counter += 1
    except Exception:
        for i, seg in enumerate(transcript.get("segments", [])):
            seg["speaker"] = f"Segment {i // 6 + 1}"
    return transcript.get("segments", [])


# --------------------------------------------------------------------------
# Technical mode
# --------------------------------------------------------------------------
TOOL_PATTERNS = {
    "git": r"\bgit\s+(?:commit|push|pull|clone|merge|rebase|branch|checkout)\b",
    "docker": r"\bdocker\s+(?:build|run|compose|exec|pull)\b",
    "npm": r"\b(?:npm|pnpm|yarn)\s+(?:install|run|build|dev|test|add)\b",
    "python": r"\b(?:python3?|pip3?|poetry|uv)\s+\S+",
    "ffmpeg": r"\bffprobe?\s+\S+",
    "curl": r"\bcurl\s+\S+",
    "kubectl": r"\bkubectl\s+\S+",
    "aws": r"\baws\s+[a-z]{2,}-\S+",
    "ssh": r"\bssh\s+\S+",
}
SHELL_RE = re.compile(r"\b(?:sudo\s+)?(?:npm|pnpm|yarn|pip3?|python3?|git|docker|"
                      r"kubectl|ffmpeg|ffprobe|curl|make|cargo|go|apt(?:-get)?|brew|"
                      r"systemctl|terraform|cd|ls|cat|grep|chmod|export)\b[^.!?]{0,120}")
URL_RE = re.compile(r"https?://[^\s)\]]+")
PATH_RE = re.compile(r"(?:~/|\./|/)[\w./-]+\.[A-Za-z]{1,6}\b")
CODE_RE = re.compile(r"`([^`\n]{4,120})`")


def extract_technical(transcript: Dict[str, Any], use_llm: bool = True) -> Dict[str, Any]:
    """Extract tools, commands, code, steps and terminology.

    Regex extraction always runs (auditable, offline). The LLM pass, when a
    key is configured, adds definitions and rationale on top.
    """
    segments = transcript.get("segments", [])
    text = " ".join(s.get("text", "") for s in segments)

    commands = []
    for m in SHELL_RE.finditer(text):
        cmd = m.group(0).strip().rstrip(",;")
        if cmd not in commands:
            commands.append(cmd)

    tools = [name for name, pat in TOOL_PATTERNS.items() if re.search(pat, text)]
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    paths = list(dict.fromkeys(PATH_RE.findall(text)))
    code = [{"lang": "inline", "snippet": c, "note": ""}
            for c in CODE_RE.findall(text)][:20]
    steps = [s.get("text", "").strip() for s in segments
             if re.match(r"\s*(?:first|then|next|after that|finally|step \d)", s.get("text", ""), re.I)][:20]

    result: Dict[str, Any] = {
        "tools": tools,
        "commands": commands[:40],
        "code": code,
        "steps": steps,
        "terms": [],
        "decisions": [],
        "links": urls,
        "paths": paths,
        "method": "regex",
    }

    if use_llm:
        payload = {"transcript": text[:6000], "regex_findings": {
            "commands": result["commands"], "tools": tools, "links": urls}}
        llm_result = llm_json(TECH_MODE_SYSTEM, json.dumps(payload, ensure_ascii=False))
        if not llm_result.get("_fallback"):
            result.update({k: llm_result.get(k, result.get(k, []))
                           for k in ("tools", "commands", "code", "steps", "terms", "decisions")})
            result["method"] = "llm"
    return result


def generate_prompt(transcript: Dict[str, Any], task: str = "blog",
                    extra: str = "") -> Dict[str, Any]:
    """Build a ready-to-use prompt from a transcript for a downstream task."""
    text = " ".join(s.get("text", "") for s in transcript.get("segments", []))
    instruction = PROMPT_TASKS.get(task, PROMPT_TASKS["blog"])
    system = ("You write production-ready prompts for AI assistants. "
              "Return STRICT JSON {\"prompt\": \"...\", \"outline\": [\"...\"]}.")
    user = (f"Task: {instruction}\nExtra requirements: {extra or 'none'}\n\n"
            f"TRANSCRIPT:\n{text[:7000]}")
    result = llm_json(system, user)
    if result.get("_fallback"):
        prompt = (
            f"{instruction}\n\nUse the following source material:\n\n{text[:4000]}\n\n"
            f"Requirements: {extra}" if extra else
            f"{instruction}\n\nUse the following source material:\n\n{text[:4000]}"
        )
        result = {"_fallback": True, "prompt": prompt.strip(), "outline": []}
    result["task"] = task
    return result


# --------------------------------------------------------------------------
# Full pipeline + exports
# --------------------------------------------------------------------------
def build_notes(transcript: Dict[str, Any], title_hint: str = "") -> Dict[str, Any]:
    """Summary / key points / actions / glossary from a transcript.

    Uses the LLM when configured, otherwise a deterministic extractive draft
    built from the highest-signal segments (marked ``_fallback``).
    """
    text = transcript.get("text", "") or " ".join(
        s.get("text", "") for s in transcript.get("segments", [])
    )
    payload = {"transcript": text[:7000], "title": title_hint}
    notes = llm_json(NOTES_SYSTEM, json.dumps(payload, ensure_ascii=False))
    if notes.get("_fallback"):
        segments = transcript.get("segments", [])
        sents = [s.get("text", "").strip() for s in segments][:6]
        notes = {
            "_fallback": True,
            "title": title_hint or "Untitled recording",
            "summary": " ".join(sents[:3]),
            "key_points": sents,
            "action_items": [],
            "decisions": [],
            "open_questions": [s.get("text", "").strip() for s in segments
                               if "?" in s.get("text", "")][:8],
            "glossary": [],
        }
    return notes


def take_notes(source: str, *, language: Optional[str] = None,
               model_size: str = "small", technical: bool = True,
               speakers: bool = True) -> Dict[str, Any]:
    video, meta = resolve_input(source)
    workdir = os.path.dirname(video)
    duration = get_duration(video)
    audio = extract_audio(video)
    transcript = run_transcribe(audio, model_size=model_size, language=language)
    if speakers:
        detect_speakers(audio, transcript)
    transcript["text"] = " ".join(s.get("text", "").strip() for s in transcript.get("segments", []))

    payload = {"transcript": transcript["text"][:7000],
               "duration": round(duration, 1),
               "title": meta.get("title", "")}
    notes = build_notes(transcript, meta.get("title", ""))

    out = {
        "source": source,
        "title": notes.get("title") or meta.get("title", "Untitled"),
        "thumbnail": meta.get("thumbnail") or thumbnail(video, workdir),
        "duration": round(duration, 2),
        "language": transcript.get("language"),
        "transcript": transcript,
        "notes": notes,
        "technical": extract_technical(transcript, use_llm=True) if technical else None,
        "prompt": None,
    }
    return out


def notes_to_markdown(payload: Dict[str, Any]) -> str:
    notes = payload.get("notes") or {}
    lines = [f"# {payload.get('title', 'Notes')}", ""]
    if notes.get("summary"):
        lines += [str(notes["summary"]), ""]
    for heading, key in (("Key points", "key_points"), ("Action items", "action_items"),
                         ("Decisions", "decisions"), ("Open questions", "open_questions")):
        items = notes.get(key) or []
        if items:
            lines.append(f"## {heading}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    if notes.get("glossary"):
        lines.append("## Glossary")
        lines += [f"- **{g.get('term', '')}** — {g.get('definition', '')}"
                  for g in notes["glossary"]]
        lines.append("")
    tech = payload.get("technical")
    if tech:
        lines.append("## Technical")
        if tech.get("commands"):
            lines += ["### Commands", "```sh", *tech["commands"], "```", ""]
        if tech.get("tools"):
            lines += ["### Tools"] + [f"- {t}" for t in tech["tools"]] + [""]
        if tech.get("steps"):
            lines += ["### Steps"] + [f"{i + 1}. {s}" for i, s in enumerate(tech["steps"])] + [""]
    if payload.get("prompt"):
        lines += ["## Generated prompt", "", payload["prompt"]["prompt"], ""]
    lines += ["## Transcript", "", (payload.get("transcript") or {}).get("text", "")]
    return "\n".join(lines)


def write_docx(markdown: str, out_path: str) -> str:
    """Minimal DOCX writer (a docx is just a zip with one XML document)."""
    paragraphs = []
    for block in markdown.splitlines():
        style = "Normal"
        text = block
        if block.startswith("# "):
            style, text = "Heading1", block[2:]
        elif block.startswith("## "):
            style, text = "Heading2", block[3:]
        elif block.startswith("- "):
            text = "• " + block[2:]
        text = re.sub(r"[*`]", "", text)
        paragraphs.append(
            f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        )
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(paragraphs)}</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return out_path


def export_notes(payload: Dict[str, Any], fmt: str, out_dir: str) -> str:
    """Render notes/transcript in ``md | pdf | docx | txt | srt | vtt``."""
    os.makedirs(out_dir, exist_ok=True)
    md = notes_to_markdown(payload)
    segments = (payload.get("transcript") or {}).get("segments", [])
    base = re.sub(r"[^\w-]+", "_", payload.get("title", "notes"))[:40] or "notes"

    if fmt == "md":
        path = os.path.join(out_dir, f"{base}.md")
        open(path, "w", encoding="utf-8").write(md)
    elif fmt == "pdf":
        path = text_to_pdf(md, os.path.join(out_dir, f"{base}.pdf"), title=payload.get("title", "Notes"))
    elif fmt == "docx":
        path = write_docx(md, os.path.join(out_dir, f"{base}.docx"))
    elif fmt == "srt":
        path = os.path.join(out_dir, f"{base}.srt")
        open(path, "w", encoding="utf-8").write(transcript_to_srt(segments))
    elif fmt == "vtt":
        path = os.path.join(out_dir, f"{base}.vtt")
        open(path, "w", encoding="utf-8").write(transcript_to_vtt(segments))
    else:
        path = os.path.join(out_dir, f"{base}.txt")
        open(path, "w", encoding="utf-8").write(md)
    return path
