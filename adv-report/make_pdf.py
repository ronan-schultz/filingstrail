#!/usr/bin/env python3
"""Render REPORT.md to PDF with METHOD.md appended as an annex.

Deliberately plain: Times body, Helvetica headings, hairline table rules, no
cover page and no color. Charts are placed where the markdown places them.

    python make_pdf.py

Writes ADV-new-registrants.pdf. Requires reportlab.
"""

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether,
                                PageBreak, PageTemplate, Paragraph, Preformatted,
                                Spacer, Table, TableStyle)

HERE = Path(__file__).parent
OUT = HERE / "ADV-new-registrants.pdf"
MARGIN = 0.95 * inch
TEXT_W = LETTER[0] - 2 * MARGIN

S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=17,
                            leading=21, spaceAfter=4),
    "sub": ParagraphStyle("sub", fontName="Times-Italic", fontSize=9.5,
                          leading=13, textColor=colors.HexColor("#555555"),
                          spaceAfter=16),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5,
                         leading=14, spaceBefore=17, spaceAfter=6),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10,
                         leading=13, spaceBefore=12, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Times-Roman", fontSize=10.5,
                           leading=15.2, spaceAfter=9, alignment=TA_JUSTIFY),
    "li": ParagraphStyle("li", fontName="Times-Roman", fontSize=10.5,
                         leading=15.2, spaceAfter=3, leftIndent=16,
                         bulletIndent=4),
    "cap": ParagraphStyle("cap", fontName="Times-Italic", fontSize=8,
                          leading=10.5, textColor=colors.HexColor("#555555"),
                          spaceBefore=5, spaceAfter=14),
    "cell": ParagraphStyle("cell", fontName="Times-Roman", fontSize=8.5,
                           leading=11),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.5,
                            leading=11),
    "pre": ParagraphStyle("pre", fontName="Courier", fontSize=8, leading=11,
                          leftIndent=10, spaceAfter=10),
}


BLOCK_START = re.compile(r"^(?:#{1,3}\s|!\[|\||```|[-*]\s|\d+\.\s)")


def inline(t: str) -> str:
    """Markdown inline formatting to reportlab markup. Escapes first."""
    t = html.escape(t, quote=False)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", t)          # links to text
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier" size="9">\1</font>', t)
    return t


def is_num(s: str) -> bool:
    """Does this cell read as a quantity, ignoring markdown emphasis?"""
    s = re.sub(r"[*`]", "", s).strip()
    return bool(re.fullmatch(r"[\s$<>+~%.,–—0-9-]*[0-9]"
                             r"[\s$<>+~%.,–—0-9-]*", s))


def make_table(rows):
    head, body = rows[0], rows[1:]
    ncol = len(head)
    # Alignment is decided per column, not per cell: one bolded or en-dashed
    # value must not knock itself out of the column's alignment. Column 0 is
    # the label column and always reads left.
    right = [False] * ncol
    for c in range(1, ncol):
        vals = [r[c] for r in body if c < len(r)]
        right[c] = bool(vals) and sum(map(is_num, vals)) / len(vals) >= 0.6

    def cell(txt, col, style):
        st = ParagraphStyle(f"c{col}", parent=style,
                            alignment=2 if right[col] else 0)
        return Paragraph(inline(txt), st)

    data = [[cell(c, i, S["cellb"]) for i, c in enumerate(head)]]
    for r in body:
        data.append([cell(c, i, S["cell"]) for i, c in enumerate(r)])

    # Width by content. Columns whose longest entry is short get their natural
    # width, so a filename never breaks mid-token; columns with long entries (a
    # 64-character checksum, prose) share whatever is left and wrap.
    def shown(c):
        return re.sub(r"[*`]", "", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", c))

    natural, per_char = [], []
    for c in range(ncol):
        cells = [r[c] for r in rows if c < len(r)]
        natural.append(max(len(shown(x)) for x in cells))
        per_char.append(5.5 if any("`" in x for x in cells[1:]) else 4.7)
    fixed = {c: natural[c] * per_char[c] + 10 for c in range(ncol) if natural[c] <= 32}
    flex = [c for c in range(ncol) if c not in fixed]
    left = TEXT_W - sum(fixed.values())
    if flex and left >= 1.1 * inch * len(flex):
        share = sum(min(natural[c], 60) for c in flex)
        widths = [fixed.get(c) or left * min(natural[c], 60) / share for c in range(ncol)]
    else:
        # Everything fits naturally (stretch to full measure) or nothing does
        # (fall back to proportional by capped length).
        base = [fixed.get(c) or min(natural[c], 32) * per_char[c] for c in range(ncol)]
        widths = [w * TEXT_W / sum(base) for w in base]

    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#cccccc")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.75, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def figure(alt, src):
    p = HERE / src
    iw, ih = ImageReader(str(p)).getSize()
    w = TEXT_W
    img = Image(str(p), width=w, height=w * ih / iw)
    return KeepTogether([Spacer(1, 4), img, Paragraph(inline(alt), S["cap"])])


def render(md: str, flow: list, top_style="title"):
    lines = md.split("\n")
    i = 0
    para: list[str] = []

    def flush():
        if para:
            flow.append(Paragraph(inline(" ".join(para)), S["body"]))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        st = ln.strip()

        if not st:
            flush()
            i += 1
            continue

        if st.startswith("```"):
            flush()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            flow.append(Preformatted("\n".join(buf), S["pre"]))
            i += 1
            continue

        m = re.match(r"^(#{1,3})\s+(.*)", st)
        if m:
            flush()
            lvl = len(m.group(1))
            style = top_style if lvl == 1 else ("h2" if lvl == 2 else "h3")
            flow.append(Paragraph(inline(m.group(2)), S[style]))
            i += 1
            continue

        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", st)
        if m:
            flush()
            flow.append(figure(m.group(1), m.group(2)))
            i += 1
            continue

        if st.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            flow.append(Spacer(1, 3))
            flow.append(make_table(rows))
            flow.append(Spacer(1, 12))
            continue

        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)", st)
        if m:
            flush()
            bullet = "•" if st[0] in "-*" else st.split(".")[0] + "."
            text = [m.group(1)]
            i += 1
            # A hard-wrapped item continues until a blank line or a new block.
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or BLOCK_START.match(nxt):
                    break
                text.append(nxt)
                i += 1
            flow.append(Paragraph(inline(" ".join(text)), S["li"],
                                  bulletText=bullet))
            continue

        para.append(st)
        i += 1

    flush()


def furniture(canvas, doc):
    canvas.saveState()
    canvas.setFont("Times-Roman", 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawCentredString(LETTER[0] / 2, 0.55 * inch, str(canvas.getPageNumber()))
    if canvas.getPageNumber() > 1:
        canvas.drawString(MARGIN, LETTER[1] - 0.62 * inch,
                          "New SEC-registered investment advisers, August 2025 to "
                          "August 2026")
        canvas.setStrokeColor(colors.HexColor("#cccccc"))
        canvas.line(MARGIN, LETTER[1] - 0.70 * inch,
                    LETTER[0] - MARGIN, LETTER[1] - 0.70 * inch)
    canvas.restoreState()


def main():
    flow: list = []
    report = (HERE / "REPORT.md").read_text(encoding="utf8")

    # Byline sits under the title, not in the markdown, which ships on its own.
    head, rest = report.split("\n", 1)
    render(head, flow)
    flow.append(Paragraph("Ronan Schultz &middot; September 10, 2026 &middot; "
                          "Source data and code: SEC Form ADV monthly extracts",
                          S["sub"]))
    render(rest, flow)

    flow.append(PageBreak())
    flow.append(Paragraph("Annex: method, sources and limits", S["title"]))
    flow.append(Paragraph("Reproduced from METHOD.md in the accompanying repository.",
                          S["sub"]))
    method = (HERE / "METHOD.md").read_text(encoding="utf8")
    method = method.split("\n", 1)[1]          # drop its own H1
    render(method, flow, top_style="h2")

    doc = BaseDocTemplate(str(OUT), pagesize=LETTER,
                          leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=0.95 * inch, bottomMargin=0.9 * inch,
                          title="New SEC-registered investment advisers, "
                                "August 2025 to August 2026",
                          author="Ronan Schultz")
    frame = Frame(MARGIN, doc.bottomMargin, TEXT_W,
                  LETTER[1] - doc.topMargin - doc.bottomMargin, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=furniture)])
    doc.build(flow)
    print(f"wrote {OUT.name} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
