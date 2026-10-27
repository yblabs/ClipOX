"""Minimal dependency-free PDF writer.

Used for summary/notes/PDF exports so the site never depends on reportlab or
weasyprint (both of which need system libraries). Produces a valid PDF 1.4
file with a Helvetica body, automatic pagination, and simple word wrapping.

Character coverage is WinAnsi (Latin-1); anything outside it is replaced with
'?' rather than corrupting the byte stream, which is the one honest limitation
of writing PDFs by hand.
"""

from __future__ import annotations

import os
import textwrap
from typing import List

PAGE_W, PAGE_H = 595.0, 842.0          # A4 in points
MARGIN = 54.0
FONT_SIZE = 10.0
LEADING = 13.5
LINES_PER_PAGE = int((PAGE_H - 2 * MARGIN) / LEADING)
CPL = 92                               # characters per line at 10pt Helvetica


def _escape(text: str) -> str:
    out = text.encode("latin-1", "replace").decode("latin-1")
    return out.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines() or [""]:
        if not raw.strip():
            lines.append("")
            continue
        indent = "  " if raw.startswith(("  ", "\t", "- ", "* ")) else ""
        lines.extend(textwrap.wrap(raw, width=CPL, initial_indent=indent,
                                   subsequent_indent=indent) or [""])
    return lines


def write_text_pdf(text: str, out_path: str, *, title: str = "Document") -> str:
    lines = _wrap(text)
    pages = [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)] or [[]]

    objects: List[str] = []
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")            # 1
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")  # 2

    font_id = 3 + len(pages) * 2
    for i, page in enumerate(pages):
        content_id = 4 + i * 2
        buf = ["BT", f"/F1 {FONT_SIZE} Tf", f"{LEADING} TL",
               f"{MARGIN} {PAGE_H - MARGIN:.1f} Td"]
        for ln in page:
            buf.append(f"({_escape(ln)}) Tj")
            buf.append("T*")
        buf.append("ET")
        stream = "\n".join(buf)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")

    objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   f"/Encoding /WinAnsiEncoding >>")
    title_id = font_id + 1
    objects.append(f"<< /Title ({_escape(title)}) /Producer (wayin-site) >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n{body}\nendobj\n".encode("latin-1", "replace")

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {title_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(out)
    return out_path
