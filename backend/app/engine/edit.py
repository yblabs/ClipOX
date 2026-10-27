"""AI Video Editor engine.

Four jobs, all of which reuse the transcription layer from ``transcribe.py``:

1. ``extract_audio`` - pull a 16 kHz mono WAV out of any container.
2. ``detect_silence`` - find dead air above a threshold (default: -30 dB for
   longer than 1.0 s) and return the exact cut timestamps.
3. ``transcribe_audio`` - word-level timestamps, with an optional
   ``keep`` parameter so a trimmed file can keep its original timing.
4. ``render_final_video`` - stitch the surviving segments back together
   *without* re-encoding when the cut points fall on keyframes, re-encoding
   with a stream-copy-friendly trim otherwise, and burn a styled ``.ass``
   caption track on top.

Text-based editing lives in ``kept_intervals``: it maps an edited transcript
back onto the original timeline, so deleting a sentence in the UI removes
exactly that slice of video.

Silence thresholds are in dBFS relative to full scale (16-bit PCM max = 32767),
so -30 dB ≈ 1/31 of full scale. Detection uses whole-frame windows (20 ms) of
the *peak* absolute sample in each window, which is far cheaper than an RMS
sweep and never misses a spike; a segment only counts as silent when *every*
frame in it stays under the threshold, so a single breath does not get cut.
"""

from __future__ import annotations

import json
import difflib
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import FFMPEG_BIN, FFPROBE_BIN
from .ass import generate_ass
from .ingest import run_transcribe

FRAME_MS = 20
SAMPLE_RATE = 16000


class EditError(RuntimeError):
    """Raised when a media operation cannot be completed."""


# --------------------------------------------------------------------------
# 1. Audio extraction
# --------------------------------------------------------------------------
def extract_audio(source: str, out_path: Optional[str] = None) -> str:
    """Extract the audio track as 16 kHz mono 16-bit WAV via FFmpeg.

    Both audio and video sources are accepted; for an audio-only file the
    video stream does not exist and FFmpeg simply copies what is there.
    """
    if not os.path.isfile(source):
        raise EditError(f"input not found: {source}")
    out_path = out_path or tempfile.mktemp(suffix=".wav", prefix="vtaudio_")
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise EditError(f"audio extraction failed: {proc.stderr[-500:]}")
    return out_path


# --------------------------------------------------------------------------
# 2. Silence detection
# --------------------------------------------------------------------------
@dataclass
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SilenceReport:
    threshold_db: float
    min_duration: float
    window_ms: int
    duration: float
    silenced: List[Silence] = field(default_factory=list)

    @property
    def kept(self) -> List[Tuple[float, float]]:
        """Complementary non-silent intervals covering the whole file."""
        spans: List[Tuple[float, float]] = []
        cursor = 0.0
        for s in self.silenced:
            if s.start - cursor > 0.01:
                spans.append((cursor, s.start))
            cursor = max(cursor, s.end)
        if self.duration - cursor > 0.01:
            spans.append((cursor, self.duration))
        return spans or [(0.0, self.duration)]

    @property
    def saved(self) -> float:
        return sum(s.duration for s in self.silenced)


def detect_silence(
    source: str,
    *,
    threshold_db: float = -30.0,
    min_duration: float = 1.0,
    window_ms: int = FRAME_MS,
) -> SilenceReport:
    """Find runs of dead air below ``threshold_db`` lasting ``min_duration``.

    Reads the PCM stream straight from FFmpeg - no scipy/numpy dependency -
    and returns the complementary kept spans as well, which is what the
    re-assembly step consumes.
    """
    if not os.path.isfile(source):
        raise EditError(f"input not found: {source}")

    window_bytes = 2 * SAMPLE_RATE * window_ms // 1000
    threshold = int((10.0 ** (threshold_db / 20.0)) * 32767)
    min_windows = max(1, int(math.ceil((min_duration * 1000.0) / window_ms)))

    proc = subprocess.Popen(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", source,
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None

    raw = b""
    windows: List[Tuple[float, bool]] = []
    idx = 0
    while True:
        chunk = proc.stdout.read(window_bytes * 4)
        if not chunk:
            break
        raw += chunk
        usable = len(raw) - (len(raw) % window_bytes)
        for off in range(0, usable, window_bytes):
            buf = raw[off:off + window_bytes]
            peak = max(abs(int.from_bytes(buf[i:i + 2], "little", signed=True))
                       for i in range(0, len(buf) - 1, 2))
            windows.append((idx * window_ms / 1000.0, peak <= threshold))
            idx += 1
        raw = raw[usable:]
    proc.stdout.close()
    proc.wait()

    total = (idx * window_ms) / 1000.0

    # merge runs of consecutive quiet windows, then filter by length
    report = SilenceReport(threshold_db=threshold_db, min_duration=min_duration,
                           window_ms=window_ms, duration=total)
    run_start: Optional[float] = None
    run_len = 0
    for t, quiet in windows:
        if quiet:
            if run_start is None:
                run_start = t
            run_len += 1
        else:
            if run_start is not None and run_len * window_ms >= min_duration * 1000:
                report.silenced.append(Silence(run_start, t))
            run_start, run_len = None, 0
    if run_start is not None and run_len * window_ms >= min_duration * 1000:
        report.silenced.append(Silence(run_start, total))
    return report


# --------------------------------------------------------------------------
# 3. Transcription (with optional original-timeline mapping)
# --------------------------------------------------------------------------
def transcribe_audio(
    audio_path: str,
    *,
    model_size: str = "small",
    language: Optional[str] = None,
    word_timestamps: bool = True,
    keep: Optional[List[Tuple[float, float]]] = None,
    device: str = "auto",
    compute_type: str = "default",
) -> Dict[str, Any]:
    """Transcribe ``audio_path`` and return word-level segments.

    When ``keep`` is supplied the file is understood to be a concatenation of
    those original spans (in order, back to back). Word times are then mapped
    back to the ORIGINAL timeline, which lets the text editor keep pointing at
    the original video.
    """
    data = run_transcribe(audio_path, model_size=model_size, language=language,
                          word_timestamps=word_timestamps,
                          device=device, compute_type=compute_type)
    if not keep:
        return data

    offsets: List[Tuple[float, float, float]] = []
    cursor = 0.0
    for start, end in keep:
        length = max(0.0, end - start)
        offsets.append((cursor, cursor + length, start))
        cursor += length

    def remap(t: float) -> float:
        for lo, hi, original_start in offsets:
            if lo <= t < hi:
                return round(original_start + (t - lo), 3)
        return round(offsets[-1][2] + (t - offsets[-1][1]), 3) if offsets else t

    for seg in data.get("segments", []):
        seg["start"] = remap(float(seg["start"]))
        seg["end"] = remap(float(seg["end"]))
        for w in seg.get("words", []) or []:
            w["start"] = remap(float(w["start"]))
            w["end"] = remap(float(w["end"]))
    return data


# --------------------------------------------------------------------------
# 4. Text-based editing: transcript schema -> kept intervals
# --------------------------------------------------------------------------
def transcript_schema(words: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """JSON schema sent to the frontend for text-based editing.

    Every word carries its own ``[start, end]`` so deleting text in the UI
    maps 1:1 onto a time range in the video.
    """
    lines: List[Dict[str, Any]] = []
    line: Dict[str, Any] = {"id": 0, "start": None, "end": None, "text": "", "words": []}
    for w in words:
        text = str(w.get("w") or w.get("word", "")).strip()
        if not text:
            continue
        w_start = float(w.get("s", w.get("start", 0.0)))
        w_end = float(w.get("e", w.get("end", w_start)))

        # break a line on a long pause, a sentence end, or a word budget so
        # the editor never shows one 46-word wall of text per segment
        prev = line["words"][-1]["w"] if line["words"] else ""
        sentence_end = bool(prev) and prev[-1] in ".!?"
        too_long = len(line["words"]) >= 14
        break_here = bool(line["words"]) and (
            (w_start - (line["end"] or 0)) > 1.2 or (sentence_end and len(line["words"]) >= 3) or too_long
        )

        if line["start"] is None:
            line["start"] = round(w_start, 3)
        if break_here:
            lines.append(line)
            line = {"id": len(lines), "start": round(w_start, 3),
                    "end": round(w_end, 3), "text": text, "words": []}
        else:
            line["end"] = round(w_end, 3)
        line["words"].append({"w": text, "s": round(w_start, 3),
                              "e": round(w_end, 3)})
        line["text"] = (line["text"] + " " + text).strip()
    if line["words"]:
        lines.append(line)
    return {"lines": lines, "schema": "1.0"}


def kept_intervals(
    original: Sequence[Dict[str, Any]],
    kept_text: str,
    *,
    min_gap: float = 0.08,
    duration: Optional[float] = None,
) -> List[Tuple[float, float]]:
    """Map an edited transcript back onto the original timeline.

    ``original`` is the word list produced by :func:`transcribe_audio`,
    ``kept_text`` is whatever the user left in the editor. Each original word
    is consumed in order by an equally-normalized token from ``kept_text``;
    consumed words contribute their timestamps, everything else is cut.

    Returns merged ``[start, end]`` spans sorted by time. Small gaps below
    ``min_gap`` are absorbed so a trimmed word does not leave a stutter.
    """
    def norm(s: str) -> str:
        return re.sub(r"[^\w']+", "", s).lower()

    source_tokens: List[str] = []
    source_spans: List[Tuple[float, float]] = []
    for word in original:
        token = norm(str(word.get("w") or word.get("word", "")))
        if not token:
            continue
        source_tokens.append(token)
        source_spans.append((float(word.get("s", word.get("start", 0.0))),
                             float(word.get("e", word.get("end", 0.0)))))

    target_tokens = [norm(t) for t in kept_text.split() if norm(t)]
    if not source_tokens or not target_tokens:
        raise EditError("nothing to render from the edited transcript")

    # Order-preserving alignment that tolerates small transcription drift
    # (the edited text may come from a slightly different Whisper pass). A
    # strict cursor match would silently drop most of the footage once one
    # token differs.
    matcher = difflib.SequenceMatcher(None, source_tokens, target_tokens, autojunk=False)
    spans: List[Tuple[float, float]] = []
    for block in matcher.get_matching_blocks():
        for i in range(block.size):
            spans.append(source_spans[block.a + i])

    matched = len(spans)
    if matched == 0:
        raise EditError("the edited transcript does not match this video's transcript")
    if matched < max(1, int(0.5 * min(len(source_tokens), len(target_tokens)))):
        # alignment is too weak to trust — fall back to a whole-line map so a
        # bad match can never delete more footage than the user asked for
        spans = [s for tok, s in zip(source_tokens, source_spans)
                 if tok in set(target_tokens)] or spans

    if not spans:
        raise EditError("no kept words matched the transcript - nothing to render")

    spans.sort()
    merged: List[Tuple[float, float]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= min_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    if duration is not None and merged:
        merged[-1] = (merged[-1][0], min(merged[-1][1], duration))
    return merged


# --------------------------------------------------------------------------
# 5. Cutting & re-assembly
# --------------------------------------------------------------------------
def probe_duration(path: str) -> float:
    out = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise EditError(f"could not read duration of {path}")


def cut_segments(
    source: str,
    spans: Sequence[Tuple[float, float]],
    out_path: str,
    *,
    reencode: bool = True,
    crf: int = 18,
) -> str:
    """Trim ``spans`` out of ``source`` and join them into one file.

    ``reencode=True`` cuts on exact frame boundaries (frame-accurate) and is
    the default for editor workflows; ``reencode=False`` uses ``-c copy`` with
    keyframe-aligned trims for lossless, near-instant cuts.
    """
    if not spans:
        raise EditError("no segments to keep")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    inputs: List[str] = []
    filters: List[str] = []
    for i, (start, end) in enumerate(spans):
        inputs += ["-ss", f"{start:.3f}", "-t", f"{max(0.05, end - start):.3f}", "-i", source]
        filters.append(f"[{i}:v]trim=start=0:end={max(0.01, end - start):.3f},"
                       f"setpts=PTS-STARTPTS[v{i}]")
        filters.append(f"[{i}:a]atrim=start=0:end={max(0.01, end - start):.3f},"
                       f"asetpts=PTS-STARTPTS[a{i}]")

    n = len(spans)
    if n == 1:
        map_args = ["-map", "[v0]", "-map", "[a0]"]
        chain = "[v0][a0]"
    else:
        map_args = ["-map", "[vc]", "-map", "[ac]"]
        filters.append("".join(f"[v{i}][a{i}]" for i in range(n)) +
                       f"concat=n={n}:v=1:a=1[vc][ac]")
        chain = "[vc][ac]"

    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-stats"] + inputs
    if reencode:
        cmd += ["-filter_complex", ";".join(filters)] + map_args + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", out_path,
        ]
    else:
        # stream copy: fast, lossless, but cuts snap to keyframes
        cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"]
        for start, end in spans:
            cmd += ["-ss", f"{start:.3f}", "-t", f"{max(0.05, end - start):.3f}", "-i", source]
        cmd += ["-filter_complex", ";".join(filters)] + map_args + [
            "-c:v", "copy", "-c:a", "aac", out_path,
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise EditError(f"cut failed: {proc.stderr[-600:]}")
    return out_path


def render_final_video(
    source: str,
    out_path: str,
    *,
    spans: Optional[Sequence[Tuple[float, float]]] = None,
    ass_path: Optional[str] = None,
    speed: float = 1.0,
    crf: int = 18,
    reencode: bool = True,
) -> str:
    """Cut ``spans`` out of ``source`` and optionally burn an ``.ass`` track.

    A single pass: the trim filtergraph and the subtitle filter are chained
    together so the video is decoded and encoded exactly once (no generation
    loss from a two-pass cut-then-burn).
    """
    if spans is None:
        spans = [(0.0, probe_duration(source))]
    if speed <= 0:
        speed = 1.0
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    filters: List[str] = []
    inputs: List[str] = []
    for i, (start, end) in enumerate(spans):
        inputs += ["-ss", f"{start:.3f}", "-t", f"{max(0.05, end - start):.3f}", "-i", source]
        seg = max(0.01, end - start)
        filters.append(f"[{i}:v]trim=start=0:end={seg:.3f},setpts=PTS-STARTPTS[v{i}]")
        filters.append(f"[{i}:a]atrim=start=0:end={seg:.3f},asetpts=PTS-STARTPTS[a{i}]")

    n = len(spans)
    if n == 1:
        v, a = "[v0]", "[a0]"
    else:
        filters.append("".join(f"[v{i}][a{i}]" for i in range(n)) +
                       f"concat=n={n}:v=1:a=1[vc][ac]")
        v, a = "[vc]", "[ac]"

    if speed != 1.0:
        filters.append(f"{v}setpts=PTS/{speed:.4f}[vs]")
        filters.append(f"{a}atempo={speed:.4f}[as]")
        v, a = "[vs]", "[as]"

    if ass_path:
        safe = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filters.append(f"{v}ass='{safe}'[vo]")
        v = "[vo]"

    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(filters),
        "-map", v, "-map", a,
    ]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "aac"]
    cmd.append(out_path)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise EditError(f"render failed: {proc.stderr[-600:]}")
    return out_path


# --------------------------------------------------------------------------
# 6. Editor API: one call for the whole edit-clip round trip
# --------------------------------------------------------------------------
def _remap_caption_lines(
    lines: List[Dict[str, Any]],
    spans: Sequence[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    """Move caption cues from the source timeline onto the OUTPUT timeline.

    When footage is cut out, everything after a cut shifts earlier. Burning
    un-shifted cues would put every caption after the first cut in the wrong
    place, so words are remapped span by span and cues that fell entirely in
    removed footage are dropped.
    """
    kept_total = sum(max(0.0, e - s) for s, e in spans)

    def map_time(t: float) -> Optional[float]:
        acc = 0.0
        for s, e in spans:
            if t <= s:
                return acc
            if t <= e:
                return acc + (t - s)
            acc += e - s
        return None

    out: List[Dict[str, Any]] = []
    for line in lines:
        start = float(line.get("start", 0.0))
        end = float(line.get("end", start))
        ns = map_time(start)
        ne = map_time(end)
        if ns is None:
            continue
        if ne is None:
            ne = kept_total
        if ne <= ns:
            continue

        words: List[Dict[str, Any]] = []
        for w in line.get("words", []) or []:
            ws, we = map_time(float(w.get("s", 0.0))), map_time(float(w.get("e", 0.0)))
            if ws is None or we is None or we <= ws:
                continue
            words.append({**w, "s": round(ws, 3), "e": round(we, 3)})

        out.append({**line, "start": round(ns, 3), "end": round(ne, 3), "words": words})
    return out


def edit_clip(
    source: str,
    out_path: str,
    *,
    kept_text: Optional[str] = None,
    spans: Optional[List[List[float]]] = None,
    caption_lines: Optional[List[Dict[str, Any]]] = None,
    style: Optional[Dict[str, Any]] = None,
    remove_silence: bool = False,
    silence_threshold_db: float = -30.0,
    silence_min_duration: float = 1.0,
    model_size: str = "small",
) -> Dict[str, Any]:
    """Single entry point behind ``POST /api/clips/edit-clip``.

    Accepts either explicit ``spans`` (the frontend computed them) or
    ``kept_text`` (the edited transcript, which this function maps back to
    time), optionally strips silence first, then re-burns captions.
    """
    duration = probe_duration(source)
    report: Optional[SilenceReport] = None

    if remove_silence:
        report = detect_silence(source, threshold_db=silence_threshold_db,
                                min_duration=silence_min_duration)
        base: List[Tuple[float, float]] = [tuple(s) for s in report.kept]
    else:
        base = [(0.0, duration)]

    if spans:
        chosen = sorted((float(s), float(e)) for s, e in spans if float(e) > float(s))
    elif kept_text is not None:
        audio = extract_audio(source)
        data = run_transcribe(audio, model_size=model_size)
        words = [w for seg in data["segments"] for w in seg.get("words", [])]
        chosen = kept_intervals(words, kept_text, duration=duration)
    else:
        chosen = base

    if remove_silence and not spans and kept_text is None:
        chosen = base

    ass_file: Optional[str] = None
    if caption_lines:
        lines = caption_lines
        if chosen != [(0.0, duration)]:
            lines = _remap_caption_lines(caption_lines, chosen)
        if lines:
            ass_file = out_path + ".ass"
            style_obj = style if isinstance(style, dict) else {}
            generate_ass(lines, style_obj, ass_file)

    render_final_video(source, out_path, spans=chosen, ass_path=ass_file)
    return {
        "output": out_path,
        "duration": round(probe_duration(out_path), 3),
        "spans": [[round(s, 3), round(e, 3)] for s, e in chosen],
        "removed_silence": round(report.saved, 3) if report else 0.0,
        "ass": ass_file,
    }
