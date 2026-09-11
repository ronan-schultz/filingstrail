#!/usr/bin/env python3
"""Print each work's page to PDF with headless Edge (or Chrome), then check it.

    python pdf.py [--browser PATH] [--tmp-dir DIR] [--preview-dir DIR]

Run after build.py. For each published work in site.json (entries with
"status": "in_progress" have no page and are skipped):

  1. copy dist/<slug>/index.html into a fresh temp dir with
     <base href="https://filingstrail.com/<slug>"> injected, so every link in the
     PDF points at the live site rather than file:///, and with the analytics tag
     removed (nothing to load when printing);
  2. print it with a headless browser on a fresh profile with every hostname
     unresolvable, so the print cannot touch the network;
  3. write dist/<slug>/<downloads.pdf> and assert, with PyMuPDF: at least 3 pages
     of real text, US Letter, the title and each `pdf_must_contain` string (the
     charts' own text) present, no text outside the printable area (nothing
     clipped), no dark page background, and the author's name absent.

--preview-dir writes every page as PNG for a human look. Exit 1 on any failure.
The PDF is the one output that is not byte-deterministic (the browser stamps
creation dates and ids).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
LETTER = (612, 792)            # points
MARGIN = 0.75 * 72             # must match @page in static/style.css

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "microsoft-edge", "google-chrome", "chromium", "chromium-browser",
]


class PdfError(Exception):
    pass


def fail(msg):
    raise PdfError(msg)


def find_browser(explicit: str | None) -> str:
    for cand in [explicit, os.environ.get("FILINGSTRAIL_BROWSER")] + BROWSERS:
        if not cand:
            continue
        if Path(cand).is_file():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    fail("no Edge or Chrome found; pass --browser PATH")


def print_to_pdf(browser: str, page: Path, out: Path, profile: Path) -> None:
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={profile}",
        "--disable-extensions", "--disable-sync", "--disable-background-networking",
        "--disable-component-update", "--disable-default-apps", "--no-pings",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        # No --blink-settings=scriptEnabled=false: it stops headless Edge from
        # printing at all. The copy being printed has no scripts anyway.
        "--run-all-compositor-stages-before-draw", "--virtual-time-budget=8000",
        "--no-pdf-header-footer", f"--print-to-pdf={out}", page.as_uri(),
    ]
    if out.exists():
        out.unlink()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    # On Windows msedge.exe is a launcher: it can return at once while the real
    # browser process is still printing. Wait for a complete, stable file.
    t0, last, stable = time.time(), -1, 0
    while time.time() - t0 < 120:
        size = out.stat().st_size if out.is_file() else -1
        stable = stable + 1 if size > 0 and size == last else 0
        last = size
        if stable >= 3 and b"%%EOF" in out.read_bytes()[-2048:]:
            break
        time.sleep(0.4)
    else:
        fail(f"browser did not write {out} (exit {r.returncode}): {r.stderr.strip()[-800:]}")
    # Let the browser release its profile before the temp dir is removed.
    lock = profile / "lockfile"
    while lock.exists() and time.time() - t0 < 150:
        try:
            lock.unlink()
        except OSError:
            time.sleep(0.5)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check_pdf(pdf: Path, title: str, must: list[str], author: str, preview: Path | None) -> str:
    doc = fitz.open(pdf)
    problems = []
    n = doc.page_count
    if n < 3:
        problems.append(f"{n} pages; expected at least 3")
    texts = [p.get_text() for p in doc]
    for i, t in enumerate(texts[:3]):
        if len(norm(t)) < 400:
            problems.append(f"page {i + 1} has only {len(norm(t))} characters of text")
    full = norm(" ".join(texts))
    for s in [title] + must:
        if norm(s) not in full:
            problems.append(f"text not found in PDF: {s!r}")
    if author in full or author in json.dumps(doc.metadata):
        problems.append("the author's name appears in the PDF")

    lo, hi = MARGIN - 3, LETTER[0] - MARGIN + 3
    for i, p in enumerate(doc):
        if abs(p.rect.width - LETTER[0]) > 2 or abs(p.rect.height - LETTER[1]) > 2:
            problems.append(f"page {i + 1} is {p.rect.width:.0f}x{p.rect.height:.0f}pt, not US Letter")
        for b in p.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    x0, _, x1, _ = sp["bbox"]
                    if sp["text"].strip() and (x0 < lo or x1 > hi):
                        problems.append(f"page {i + 1}: text outside the printable area "
                                        f"({x0:.0f}-{x1:.0f}pt): {sp['text'][:40]!r}")
        pix = p.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), colorspace=fitz.csGRAY)
        px = pix.samples
        mean = sum(px) / len(px)
        corner = sum(px[r * pix.stride + c] for r in range(4) for c in range(4)) / 16
        if mean < 200 or corner < 245:
            problems.append(f"page {i + 1} looks dark (mean {mean:.0f}, corner {corner:.0f} of 255)")
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
            p.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(preview / f"{pdf.stem}-p{i + 1}.png")
    doc.close()
    if problems:
        fail(f"{pdf.name}:\n    " + "\n    ".join(dict.fromkeys(problems)))
    return f"{n} pages, {pdf.stat().st_size:,} bytes"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print dist/<slug>/index.html to PDF and check it.")
    ap.add_argument("--browser", help="path to msedge/chrome (default: auto-detect)")
    ap.add_argument("--tmp-dir", type=Path, help="parent for the temp page copy and browser profile")
    ap.add_argument("--preview-dir", type=Path, help="write every page as PNG here")
    args = ap.parse_args(argv)

    site = json.loads((HERE / "site.json").read_text(encoding="utf-8"))
    base = site["base_url"].rstrip("/")
    browser = find_browser(args.browser)
    if args.tmp_dir:
        args.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="filingstrail-pdf-", dir=args.tmp_dir))
    try:
        for w in site["works"]:
            if w.get("status", "published") != "published":
                continue
            src = DIST / w["slug"] / "index.html"
            if not src.is_file():
                fail(f"{src} missing; run build.py first")
            page = src.read_text(encoding="utf-8")
            charset = '<meta charset="utf-8">'
            tag = f'<script defer src="{site["analytics_src"]}"></script>\n'
            if page.count(charset) != 1 or page.count(tag) != 1:
                fail(f"{src}: expected one charset meta and one analytics tag")
            page = page.replace(charset, f'{charset}\n<base href="{html.escape(base)}/{w["slug"]}">')
            page = page.replace(tag, "")
            work_dir = tmp / w["slug"]
            work_dir.mkdir()
            (work_dir / "index.html").write_bytes(page.encode("utf-8"))

            out = DIST / w["slug"] / w["downloads"]["pdf"]
            print_to_pdf(browser, work_dir / "index.html", out, tmp / f"profile-{w['slug']}")
            title = html.unescape(re.search(r"<h1>(.*?)</h1>", page, re.S)[1])
            title = re.sub(r"<[^>]+>", "", title)
            summary = check_pdf(out, title, w.get("pdf_must_contain", []), site["author"],
                                args.preview_dir)
            print(f"  {out.relative_to(DIST).as_posix()}: {summary}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PdfError as e:
        print(f"PDF FAILED: {e}", file=sys.stderr)
        sys.exit(1)
