#!/usr/bin/env python3
"""Self-test for verify.py: build a known-good fixture site, prove it PASSES, then plant
one defect at a time and prove the gate FAILS on the right check.

    python site/checks/selftest.py [--work DIR] [--quick] [--only NAME ...] [--jobs N]

The fixture is built from the real, read-only sources: adv-report/REPORT.md and METHOD.md
rendered with python-markdown, archive/MANIFEST.csv for /data, hand-made inline chart SVGs
carrying the real chart text, a PDF printed from the fixture /adv page with headless
Edge/Chrome, and copies of the real firm-level CSV and manifest. The authored intros
(content/*.md) are written from the copy pinned in checks/pages.json. Everything is written
under --work (default: a temp dir), never into site/dist or site/content.

Besides the per-mutation runs, an in-process smoke test drives run_live() (the --live code
path) against the fixture through a stubbed fetcher - no network - and proves N, M and D
run on the served bytes and catch a planted defect there.
"""
from __future__ import annotations

import argparse
import contextlib
import email.message
import html
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent          # site/checks
SITE = HERE.parent                              # site
REPO = SITE.parent                              # ria-research
sys.path.insert(0, str(SITE))
sys.dont_write_bytecode = True  # keep site/ free of __pycache__
import verify  # noqa: E402

PAGES = json.loads((HERE / "pages.json").read_text(encoding="utf-8"))
SITECHK = json.loads((HERE / "site.json").read_text(encoding="utf-8"))
REPO_URL = SITECHK["repo_url"]
PDF_NAME = "adv-new-registrants-2026-09-10.pdf"
TAG = '<script defer src="/_vercel/insights/script.js"></script>'
MANIFEST = REPO / "archive" / "MANIFEST.csv"
METHOD_MD = REPO / "adv-report" / "METHOD.md"
STATS_LOG = SITE / "build" / "adv" / "report_stats.log"
HALO = ".chart text{paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round}"
PRINT_HIDE = "@media print{.masthead,footer{display:none}}"

CSS = """:root{--bg:#fbfaf8;--fg:#1a1a1a;--muted:#666;
--chart-ink:#1a1a1a;--chart-ink-2:#444444;--chart-muted:#666666;--chart-bar:#7a8aa0;--chart-alert:#c05746;--chart-key:#3e5a78}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#e6e3de;--muted:#a29e97;
--chart-ink:#e6e3de;--chart-ink-2:#bdb9b2;--chart-muted:#a29e97;--chart-bar:#8595ab;--chart-alert:#dd7866;--chart-key:#a9c3e0}}
html{background:var(--bg);color:var(--fg)}
body{margin:0 auto;max-width:42rem;padding:1rem;font:17px/1.55 Georgia,serif;overflow-wrap:break-word}
a,code{overflow-wrap:anywhere}
.masthead{display:flex;flex-wrap:wrap;gap:.25rem 1.25rem;align-items:baseline;border-bottom:1px solid #ccc;padding-bottom:.5rem}
.wordmark{font-variant:small-caps;letter-spacing:.04em;font-weight:600;font-size:1rem;margin:0}
.masthead nav a{margin-right:.8rem;color:var(--muted);text-decoration:none}
.masthead nav a[aria-current]{text-decoration:underline}
.table-scroll,.chart-scroll{overflow-x:auto;max-width:100%}
pre{overflow-x:auto;max-width:100%}pre code{overflow-wrap:normal}
table{border-collapse:collapse}td,th{padding:.2rem .6rem;border-bottom:1px solid #ccc}
th.num,td.num{text-align:right}
.chart-scroll svg{display:block}
""" + HALO + """
.status,.desc,.note{color:var(--muted);font-size:.9em}
.lede{font-size:1.1em}
footer{margin-top:2rem;color:var(--muted)}
@media print{:root{--bg:#fff;--fg:#000}}
""" + PRINT_HIDE + "\n"


def text_el(x, y, s, size=10, fill="--chart-ink", lit="#000000", anchor="start", tid=None):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<text x="{x}" y="{y}" style="font: {size}px sans-serif; text-anchor: {anchor}; '
            f'fill: {lit}; fill: var({fill}, {lit})">{s}</text>')


def chart_svg(prefix, title, subtitle, groups, bars, notes, ylabel, label_id):
    """A matplotlib-shaped inline SVG: one <text> per line, prefixed ids, CSS-var colours."""
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" class="chart" '
             f'width="612" height="360" viewBox="0 0 612 360" role="img" aria-labelledby="{label_id}">',
             f'<title id="{label_id}">{title}</title>',
             f'<metadata>Matplotlib v3.10.8 2026-09-10T00:00:00</metadata>',
             f'<defs><clipPath id="{prefix}-clip"><rect x="50" y="40" width="540" height="240"/></clipPath>'
             f'<path id="{prefix}-tick" d="M 0 0 L 0 3.5"/></defs>',
             f'<g id="{prefix}-figure">',
             text_el(50, 18, title, 12), text_el(50, 34, subtitle, 11)]
    n = len(groups)
    for i, (lab1, lab2) in enumerate(groups):
        cx = 80 + i * (520 / n)
        parts.append(text_el(cx, 300, lab1, 10, anchor="middle"))
        parts.append(text_el(cx, 314, lab2, 10, anchor="middle"))
        parts.append(f'<use xlink:href="#{prefix}-tick" x="{cx}" y="282" style="stroke: #000000; stroke: var(--chart-ink, #000000)"/>')
    for (i, h, lab, col, lit) in bars:
        cx = 80 + i * (520 / n)
        parts.append(f'<path clip-path="url(#{prefix}-clip)" d="M {cx - 12} 280 L {cx - 12} {280 - h * 4} '
                     f'L {cx + 12} {280 - h * 4} L {cx + 12} 280 z" style="fill: {lit}; fill: var({col}, {lit})"/>')
        parts.append(text_el(cx, 276 - h * 4, lab, 9, anchor="middle"))
    parts.append(text_el(14, 160, ylabel, 10))
    for k, line in enumerate(notes):
        parts.append(text_el(50, 332 + 12 * k, line, 7.5, "--chart-muted", "#666666"))
    parts.append("</g></svg>")
    return "\n".join(parts)


NW_SVG = chart_svg(
    "nw", "New SEC-registered advisers are twice as likely to have no website",
    "1,693 firms registered Aug 1, 2025 to Aug 31, 2026, by reported regulatory AUM",
    [("$0 reported", "n=309"), ("<$25M", "n=54"), ("$25-100M", "n=111"), ("$100-500M", "n=1,046"),
     ("$500M-1B", "n=83"), (">$1B", "n=90")],
    [(0, 32, "32%", "--chart-alert", "#c05746"), (1, 13, "13%", "--chart-bar", "#7a8aa0"),
     (2, 13, "13%", "--chart-bar", "#7a8aa0"), (3, 13, "13%", "--chart-bar", "#7a8aa0"),
     (4, 7, "7%", "--chart-bar", "#7a8aa0"), (5, 11, "11%", "--chart-bar", "#7a8aa0")],
    ["Source: SEC Form ADV monthly registered-adviser extracts, diffed on Organization CRD#. "
     "AUM = Item 5F(2)(c); website = Item 1.I.", "all registered advisers: 8.1%"],
    "share with no website", "nw-title")

HC_SVG = chart_svg(
    "hc", "Headcount sorts the pre-launch cohort; website presence does not",
    "628 SEC-registered advisers reporting $0 AUM as of Jul 31, 2025, tracked to Aug 31, 2026",
    [("1", "n=128"), ("2-5", "n=265"), ("6-20", "n=134"), ("21+", "n=101")],
    [(0, 26, "26%", "--chart-bar", "#7a8aa0"), (1, 45, "45%", "--chart-key", "#3e5a78"),
     (2, 31, "31%", "--chart-bar", "#7a8aa0"), (3, 20, "20%", "--chart-bar", "#7a8aa0")],
    ["Source: SEC Form ADV monthly registered-adviser extracts. Conversion gap between 1 and 2-5 employees: "
     "z=3.58, p=0.0003.", "The 21+ band is not a failure rate: firms that large report $0 for structural "
                          "reasons rather than because they are pre-launch, which its 4% deregistration rate shows."],
    "share of cohort", "hc-title")


# ------------------------------------------------------------------ fixture pages

CURRENT = {"adv/index.html": ("Work", "true"), "method/index.html": ("Method", "page"),
           "data/index.html": ("Data", "page")}


def masthead(rel: str) -> str:
    cur = CURRENT.get(rel)
    links = " ".join(f'<a href="{h}"' + (f' aria-current="{cur[1]}"' if cur and cur[0] == t else "") + f">{t}</a>"
                     for t, h in PAGES["masthead"]["nav"])
    wm = ('<h1 class="wordmark"><a href="/" aria-current="page">Filings Trail</a></h1>' if rel == "index.html"
          else '<a class="wordmark" href="/">Filings Trail</a>')
    return f'<header class="masthead">{wm}\n<nav aria-label="Site">{links}</nav></header>'


FOOTER = f'<footer><p>Filings Trail · Built from public SEC filings · <a href="{REPO_URL}">Code on GitHub</a></p></footer>'
ROOT_FOOTER = ('<footer><p>Built and maintained by Ronan Schultz · <a href="mailto:ronanschultz35@gmail.com">'
               'ronanschultz35@gmail.com</a></p></footer>')


def page(rel, title, main, desc="Filings Trail", canonical="/"):
    footer = ROOT_FOOTER if rel == "index.html" else FOOTER
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{html.escape(title, quote=False)}</title>\n<meta name="description" content="{html.escape(desc)}">\n'
            f'<link rel="stylesheet" href="/assets/site.css">\n'
            f'<link rel="canonical" href="https://filingstrail.com{canonical}">\n'
            f'{TAG}\n</head>\n<body>\n{masthead(rel)}\n{main}\n{footer}\n</body>\n</html>\n')


def md(text: str, smarty=True) -> str:
    import markdown
    return markdown.markdown(text, extensions=["tables", "fenced_code"] + (["smarty"] if smarty else []),
                             output_format="html")


def wrap_tables(h: str) -> str:
    return h.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>")


_TRACKED = None


def link_code(h: str) -> str:
    """Link code spans that name a committed file (repo root or adv-report/), as build.py does."""
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = verify.git_tracked(REPO) or set()

    def sub(m):
        t = m.group(1)
        hit = next((p for p in (t, "adv-report/" + t) if p in _TRACKED), None)
        return f'<a href="{REPO_URL}/blob/master/{hit}">{m.group(0)}</a>' if hit else m.group(0)
    return re.sub(r"(?<!\">)<code>([A-Za-z0-9_][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]+)</code>", sub, h)


def align_numeric(part: str) -> str:
    """Right-align (---:) every table column whose body cells are all numeric."""
    lines = part.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|(\s*:?-+:?\s*\|)+", lines[i + 1].strip()):
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines[i + 2:j]]
            sep = [c.strip() for c in lines[i + 1].strip().strip("|").split("|")]
            for c in range(1, len(sep)):
                vals = [verify.norm(verify.md_inline(r[c])) for r in rows if c < len(r)]
                if vals and all(verify.NUMERIC_CELL.fullmatch(v) for v in vals):
                    sep[c] = "---:"
            lines[i + 1] = "| " + " | ".join(sep) + " |"
            i = j
        else:
            i += 1
    return "\n".join(lines)


def method_main() -> str:
    ms = PAGES["method"]
    lines = METHOD_MD.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    run = next(ln for ln in lines if ln.startswith(ms["run_line_prefix"]))
    part = "\n".join(lines[lines.index(ms["extract_from"]):])
    paras = part.split("\n\n")
    drop = ms["dropped_sentence"]
    hit = 0
    for k, p in enumerate(paras):
        flat = " ".join(p.split())
        if drop in flat:
            paras[k] = flat.replace(" " + drop, "", 1)
            hit += 1
    assert hit == 1, "dropped sentence not found in METHOD.md"
    part = "\n\n".join(paras)
    part = re.sub(r"^## ", "### ", part, flags=re.M)
    for old, new in ms["heading_renames"].items():
        assert ("### " + old) in part
        part = part.replace("### " + old, "### " + new)
    part = align_numeric(part)
    intro = md("\n".join(ms["intro_expected"]))
    body = link_code(wrap_tables(md(run + "\n\n" + part)))
    return f'<main>\n{intro}\n<h2>{ms["h2"]}</h2>\n{body}\n</main>'


def data_main() -> str:
    ds = PAGES["data"]
    man = verify.read_manifest(MANIFEST)
    stale = verify.stale_files(man)
    out = [md("\n".join(ds["intro_expected"]))]
    for heading, variant in ds["sections"]:
        rows = sorted((r for r in man if r["variant_by_content"] == variant), key=lambda r: r["vintage"], reverse=True)
        trs = []
        for r in rows:
            fn = r["filename"]
            panel = fn in ds["panel_files"]
            name = f"<strong>{fn}</strong>" if panel else fn
            dag = " †" if fn in stale else ""
            src = ds["source_labels"][r["source"]]
            trs.append(f'<tr{" class=" + chr(34) + ds["panel_class"] + chr(34) if panel else ""}>'
                       f'<td><a href="{html.escape(r["url"])}">{name}</a></td><td>{r["vintage"]}</td>'
                       f'<td>{r["data_through"]}{dag}</td><td class="num">{int(r["n_rows"]):,}</td><td>{src}</td>'
                       f'<td><code title="{r["sha256"]}">{r["sha256"][:ds["sha_prefix_len"]]}</code></td></tr>')
        out.append(f'<h2>{heading}</h2>\n<div class="table-scroll"><table>\n<thead><tr><th>File</th><th>File date</th>'
                   f'<th>Data through</th><th class="num">Rows</th><th>Source</th><th>sha256</th></tr></thead>\n<tbody>\n'
                   + "\n".join(trs) + "\n</tbody></table></div>")
    out.append(f'<p class="note">{ds["note"]}</p>')
    return "<main>\n" + "\n".join(out) + "\n</main>"


def adv_sources_section() -> str:
    lines = METHOD_MD.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    a, b = lines.index("## Sources"), lines.index("### The file-selection trap")
    block = "\n".join(lines[a + 1:b]).replace(" See the date trap below.", "").replace("See the date trap below.", "")
    return (f'<h2 id="sources">Sources</h2>\n{wrap_tables(md(block, smarty=False))}\n'
            f'<p class="note">Reproduced from <code>METHOD.md</code>.</p>')


def root_main() -> str:
    rt = PAGES["root"]
    p = rt["paragraphs"]
    items = []
    for w in rt["works"]:
        if w.get("status") == "in_progress":
            items.append(f'<li class="in-progress">{w["title"]} <span class="status">{rt["in_progress_label"]}</span>'
                         + (f' <span class="desc">{w["descriptor"]}</span>' if w.get("descriptor") else "") + "</li>")
        else:
            items.append(f'<li><a href="{w["href"]}">{w["title"]}</a> <span class="desc">{w["descriptor"]}</span></li>')
    return (f'<main>\n<p class="lede">{p[0]}</p>\n<p>{p[1]}</p>\n<p>{p[2]}</p>\n'
            f'<h2 id="{rt["work_heading_id"]}">{rt["work_heading_text"]}</h2>\n<ul class="works">\n'
            + "\n".join(items) + "\n</ul>\n</main>")


def build_fixture(root: Path, tmp: Path) -> None:
    import markdown
    dst, tmp = (root / "dist").resolve(), tmp.resolve()
    if root.exists():
        shutil.rmtree(root)
    for sub in ("adv", "assets", "method", "data"):
        (dst / sub).mkdir(parents=True)
    rmd = (REPO / "adv-report" / "REPORT.md").read_text(encoding="utf-8")
    body = markdown.markdown(rmd, extensions=["tables"])
    body = re.sub(r"<p><img[^>]*conversion_by_headcount\.png[^>]*></p>",
                  lambda m: f'<figure id="fig-headcount"><div class="chart-scroll">{HC_SVG}</div>'
                            f'<figcaption>Conversion and deregistration by July 2025 headcount.</figcaption></figure>', body)
    body = re.sub(r"<p><img[^>]*new_registrants\.png[^>]*></p>",
                  lambda m: f'<figure id="fig-no-website"><div class="chart-scroll">{NW_SVG}</div>'
                            f'<figcaption>No-website share by AUM band.</figcaption></figure>', body)
    assert "<img" not in body
    body = body.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>")
    h1_end = body.index("</h1>") + len("</h1>")
    meta = (f'\n<p class="links"><a href="/adv/{PDF_NAME}">PDF</a> · '
            f'<a href="/adv/new_registrants.csv">Firm-level CSV</a> · <a href="{REPO_URL}">Repository</a></p>')
    body = link_code(body[:h1_end] + meta + body[h1_end:] + "\n" + adv_sources_section())
    (dst / "adv" / "index.html").write_text(
        page("adv/index.html", "Two-thirds of pre-launch RIAs had launched thirteen months later - Filings Trail",
             f"<main>\n{body}\n</main>", canonical="/adv"), encoding="utf-8")
    (dst / "index.html").write_text(page("index.html", "Filings Trail", root_main()), encoding="utf-8")
    (dst / "method" / "index.html").write_text(
        page("method/index.html", PAGES["method"]["title"], method_main(), PAGES["method"]["description"], "/method"),
        encoding="utf-8")
    (dst / "data" / "index.html").write_text(
        page("data/index.html", PAGES["data"]["title"], data_main(), PAGES["data"]["description"], "/data"),
        encoding="utf-8")
    (dst / "404.html").write_text(page("404.html", "Not found - Filings Trail",
                                       '<main><h1>Not found</h1><p><a href="/">Filings Trail</a></p></main>'),
                                  encoding="utf-8")
    (dst / "assets" / "site.css").write_text(CSS, encoding="utf-8")
    (dst / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: https://filingstrail.com/sitemap.xml\n",
                                    encoding="utf-8")
    (dst / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"<url><loc>https://filingstrail.com{p if p != '/' else '/'}</loc></url>\n" for p in PAGES["sitemap"])
        + '</urlset>\n', encoding="utf-8")
    (dst / "vercel.json").write_text(json.dumps({"cleanUrls": True, "trailingSlash": False}, indent=2),
                                     encoding="utf-8")
    shutil.copyfile(REPO / "adv-report" / "new_registrants.csv", dst / "adv" / "new_registrants.csv")
    shutil.copyfile(MANIFEST, dst / "data" / "manifest.csv")
    content = root / "content"
    content.mkdir()
    (content / PAGES["method"]["intro_md"]).write_text("\n".join(PAGES["method"]["intro_expected"]) + "\n", encoding="utf-8")
    (content / PAGES["data"]["intro_md"]).write_text("\n".join(PAGES["data"]["intro_expected"]) + "\n", encoding="utf-8")
    (root / "site.json").write_text(json.dumps({"name": "Filings Trail", "repo_url": REPO_URL, "author": "Ronan Schultz",
                                                "email": "ronanschultz35@gmail.com",
                                                "base_url": "https://filingstrail.com"}), encoding="utf-8")
    print_pdf(dst, tmp)


def print_pdf(dst: Path, tmp: Path) -> None:
    out = dst / "adv" / PDF_NAME
    bundle = verify.Bundle.from_dist(dst)
    srv = verify.LocalServer(bundle, "/_vercel/insights/script.js")
    try:
        br = verify.Browser(tmp)
        if not br.select(srv):
            raise SystemExit("no headless browser for the fixture PDF: " + "; ".join(br.notes))
        br._run(br.exe, [f"--print-to-pdf={out}", "--no-pdf-header-footer"], srv.url("/adv"), expect_file=out)
    finally:
        srv.close()
    if not out.exists():
        raise SystemExit("fixture PDF was not written")


# ------------------------------------------------------------------ mutations

def edit(path: Path, old: str, new: str, count=1):
    s = path.read_text(encoding="utf-8")
    if old not in s:
        raise AssertionError(f"mutation anchor not found in {path.name}: {old[:60]!r}")
    path.write_text(s.replace(old, new, count), encoding="utf-8")


def resub(path: Path, rx: str, new: str, flags=re.S):
    s = path.read_text(encoding="utf-8")
    s2, n = re.subn(rx, new, s, count=1, flags=flags)
    if not n:
        raise AssertionError(f"mutation regex matched nothing in {path.name}: {rx[:60]!r}")
    path.write_text(s2, encoding="utf-8")


def m_pdf_text(d: Path, text: str):
    fitz = verify._fitz()
    p = d / "adv" / PDF_NAME
    doc = fitz.open(str(p))
    doc[0].insert_text((72, 60), text, fontsize=9)
    data = doc.tobytes()
    doc.close()
    p.write_bytes(data)


def m_print_label_on_bar(d: Path, label_rgb=(0x44, 0x44, 0x44)):
    """Print drops the halo (a print-only stroke:none) and the PDF has a chart-style
    label drawn over a --chart-bar fill: #444 on #7a8aa0 is 2.77:1."""
    edit(d / CSSF, PRINT_HIDE, PRINT_HIDE + "\n@media print{.chart text{stroke:none}}")
    fitz = verify._fitz()
    p = d / "adv" / PDF_NAME
    doc = fitz.open(str(p))
    page = doc[0]
    page.draw_rect(fitz.Rect(60, 90, 260, 130), color=None, fill=(0x7a / 255, 0x8a / 255, 0xa0 / 255))
    page.insert_text((70, 114), "label over a bar", fontsize=9, color=tuple(c / 255 for c in label_rgb))
    data = doc.tobytes()
    doc.close()
    p.write_bytes(data)


def m_log(d: Path, old: str, new: str):
    """Copy the real report_stats.log into the mutation dir with one edit; point M3 at it."""
    s = STATS_LOG.read_text(encoding="utf-8")
    assert old in s, f"log anchor {old!r} not found"
    p = d.parent / "report_stats.log"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    return {"args": ["--stats-log", str(p)]}


def m_manifest_byte(d: Path):
    p = d / "data" / "manifest.csv"
    b = p.read_bytes()
    i = b.index(b"backfill")
    p.write_bytes(b[:i] + b"backfilL" + b[i + 8:])


ADV = Path("adv/index.html")
ROOT = Path("index.html")
METH = Path("method/index.html")
DATA = Path("data/index.html")
E404 = Path("404.html")
CSSF = Path("assets/site.css")
SHA_0402 = "314c99c72cf2629e5760728a1bcab6b1701b8ca4bade0e436f6c1237db5f5f87"   # ia040224.zip
SHA_PRIOR = "977d83fdd67acdc464d30d892fe1a839fae2d8d6a47661ecb7ec53bd0e7c312c"  # ia09022025.xlsx
DROPPED = PAGES["method"]["dropped_sentence"]

MUTATIONS = [
    # (name, description, mutate(dist, site_json) -> None | {"args": [...]}, expected [(check-id, substring)],
    #  needs_browser, extra args)
    # ---- existing contract
    ("forbidden-html", 'plant "twelve months" in /adv prose',
     lambda d, sj: edit(d / ADV, "<p>That number is why", "<p>It ran twelve months. That number is why"),
     [("B", "twelve months"), ("A-W", "twelve")], False, []),
    ("forbidden-pdf", 'plant "1,573 validated firms (92.9%)" in the PDF only',
     lambda d, sj: m_pdf_text(d, "An earlier version reported 1,573 validated firms (92.9%)."),
     [("B", '"1,573"'), ("B", '"92.9%"')], False, []),
    ("forbidden-csv", 'plant "120 re-registrations" in a CSV cell',
     lambda d, sj: edit(d / "adv/new_registrants.csv", "ALEXANDRIA", "ALEXANDRIA 120 re-registrations"),
     [("B", "120 re-registrations")], False, []),
    ("forbidden-svg", 'plant "September to September" in chart <text>',
     lambda d, sj: edit(d / ADV, "share of cohort</text>", "share of cohort September to September</text>"),
     [("B", "September to September")], False, []),
    ("forbidden-entity", 'plant "16&#46;2%" (entity-encoded) in an attribute',
     lambda d, sj: edit(d / ADV, '<main>', '<main data-note="16&#46;2% of firms">'),
     [("B", "16.2%")], False, []),
    ("forbidden-manifest", 'plant "16.2%" in data/manifest.csv',
     lambda d, sj: edit(d / "data/manifest.csv", "exempt,exempt,ERA", "exempt 16.2%,exempt,ERA"),
     [("B", "16.2%"), ("D4", "!=")], False, []),
    ("headcount-cell", "2-5 converted 44.5% -> 45.5% in the table only",
     lambda d, sj: edit(d / ADV, "<td><strong>44.5%</strong></td>", "<td><strong>45.5%</strong></td>"),
     [("A8", "Headcount bands")], False, []),
    ("table-rows", "drop the 21+ row from the headcount table",
     lambda d, sj: edit(d / ADV, "<tr>\n<td>21+</td>\n<td>101</td>\n<td>19.8%</td>\n<td>4.0%</td>\n</tr>", ""),
     [("A8", "Headcount bands")], False, []),
    ("z-value", "headcount z = 3.58 -> 3.59 in prose",
     lambda d, sj: edit(d / ADV, "z = 3.58", "z = 3.59"),
     [("A9", "Tests")], False, []),
    ("z-swap", "swap the conversion and deregistration test statistics",
     lambda d, sj: (edit(d / ADV, "(z = 3.58, p = 0.0003)", "(z = TMP)"),
                    edit(d / ADV, "(z = 3.25,\np = 0.0012)", "(z = 3.58, p = 0.0003)"),
                    edit(d / ADV, "(z = TMP)", "(z = 3.25, p = 0.0012)")) and None,
     [("A9", "Tests")], False, []),
    ("window", '"The window runs thirteen months" -> "twelve months"',
     lambda d, sj: edit(d / ADV, "The window runs thirteen months", "The window runs twelve months"),
     [("A1", "Window"), ("B", "twelve months")], False, []),
    ("validated", '"1,689 of 1,693 (99.8%)" -> "1,573 of 1,693 (92.9%)"',
     lambda d, sj: edit(d / ADV, "1,689 of 1,693\n(99.8%)", "1,573 of 1,693\n(92.9%)"),
     [("A4", "Validated"), ("B", "1,573")], False, []),
    ("missing-chart", "remove the headcount chart",
     lambda d, sj: resub(d / ADV, r'<figure id="fig-headcount">.*?</figure>', ""),
     [("A11", "headcount chart")], False, []),
    ("img-chart", "headcount chart as <img src=...png>",
     lambda d, sj: (resub(d / ADV, r'<div class="chart-scroll"><svg[^>]*aria-labelledby="hc-title".*?</svg></div>',
                          '<img src="/assets/adv/conversion_by_headcount.png" alt="chart">'),
                    (d / "assets/adv").mkdir(parents=True, exist_ok=True),
                    (d / "assets/adv/conversion_by_headcount.png").write_bytes(b"\x89PNG\r\n\x1a\n")) and None,
     [("A11", "no inline <svg>"), ("A12", ".png")], False, []),
    ("chart-n", "headcount chart n=128 -> n=129",
     lambda d, sj: edit(d / ADV, ">n=128<", ">n=129<"),
     [("A11", "n=128")], False, []),
    ("chart-title", 'no-website chart title "Aug 1, 2025" -> "Sep 2, 2025"',
     lambda d, sj: edit(d / ADV, "registered Aug 1, 2025 to", "registered Sep 2, 2025 to"),
     [("A10", "no single <text> line")], False, []),
    ("root-figure", 'root page mentions "1,693 firms" and "32%"',
     lambda d, sj: edit(d / ROOT, "</ul>", "<li>1,693 firms, 32% without a website.</li></ul>"),
     [("A-R", "1,693"), ("A-R", "32%")], False, []),
    ("external-script", "add <script src=https://cdn.example.com/x.js>",
     lambda d, sj: edit(d / ADV, "</head>", '<script src="https://cdn.example.com/x.js"></script></head>'),
     [("E1", "another host"), ("E1", "<script> tags")], False, []),
    ("inline-script", "add an inline <script>",
     lambda d, sj: edit(d / ROOT, "</body>", "<script>document.title='x'</script></body>"),
     [("E1", "inline script")], False, []),
    ("json-ld", "add <script type=application/ld+json>",
     lambda d, sj: edit(d / ROOT, "</head>", '<script type="application/ld+json">{"@type":"Person"}</script></head>'),
     [("E1", "inline script")], False, []),
    ("handler+storage", 'add onclick="localStorage.x=1"',
     lambda d, sj: edit(d / ROOT, '<ul class="works">', '<ul class="works" onclick="localStorage.x=1">'),
     [("E1", "event-handler"), ("E3", "localStorage")], False, []),
    ("hidden+noscript", "add <div hidden> and <noscript>",
     lambda d, sj: edit(d / ROOT, "</main>", '<div hidden>x</div><noscript>no js</noscript></main>'),
     [("E1", "hidden attribute"), ("E1", "<noscript>")], False, []),
    ("iframe", "embed an <iframe> from another host",
     lambda d, sj: edit(d / ROOT, "</main>", '<iframe src="https://example.com/embed"></iframe></main>'),
     [("E1", "<iframe>")], False, []),
    ("css-import", "@import a Google font + @font-face",
     lambda d, sj: edit(d / CSSF, ":root{", "@import url(https://fonts.googleapis.com/css?family=Inter);\n"
                                            "@font-face{font-family:X;src:url(/f.woff2)}\n:root{"),
     [("E2", "@import"), ("E2", "@font-face")], False, []),
    ("oversize", "pad /adv with a 300 KB comment",
     lambda d, sj: edit(d / ADV, "</body>", "<!-- " + ("lorem ipsum dolor " * 17000) + " --></body>"),
     [("D", "adv/index.html")], False, []),
    ("oversize-data", "pad /data with a 300 KB comment (weight covers the new pages)",
     lambda d, sj: edit(d / DATA, "</body>", "<!-- " + ("lorem ipsum dolor " * 17000) + " --></body>"),
     [("D", "data/index.html")], False, []),
    ("name-twice", '<meta name="author" content="Ronan Schultz"> on /adv',
     lambda d, sj: edit(d / ADV, "</head>", '<meta name="author" content="Ronan Schultz"></head>'),
     [("C1", "appears 2 times")], False, []),
    ("name-not-last", "root page: name no longer on the last line",
     lambda d, sj: edit(d / ROOT, "</footer>", "</footer><p>Built with care.</p>"),
     [("C1", "last line")], False, []),
    ("email-on-adv", "email address also on /adv",
     lambda d, sj: edit(d / ADV, "</main>", "<p>Contact: ronanschultz35@gmail.com</p></main>"),
     [("C2", "email")], False, []),
    ("relative-link", 'root page links "adv" (relative)',
     lambda d, sj: edit(d / ROOT, 'href="/adv"', 'href="adv"'),
     [("G1", "relative URL")], False, []),
    ("broken-link", "/adv links /adv/missing.csv",
     lambda d, sj: edit(d / ADV, 'href="/adv/new_registrants.csv"', 'href="/adv/missing.csv"'),
     [("G1", "does not resolve")], False, []),
    ("broken-fragment", "/method links /adv#nope",
     lambda d, sj: edit(d / METH, 'href="/adv#sources"', 'href="/adv#nope"'),
     [("G1", 'no id="nope"')], False, []),
    ("duplicate-id+aria", "duplicate the chart clipPath id; aria-labelledby to a missing id",
     lambda d, sj: (edit(d / ADV, 'id="nw-clip"', 'id="hc-clip"'),
                    edit(d / ADV, 'aria-labelledby="nw-title"', 'aria-labelledby="nope"'),
                    edit(d / ADV, 'url(#nw-clip)', 'url(#hc-clip)', count=99)) and None,
     [("G1", "duplicate id"), ("G1", "aria-labelledby")], False, []),
    ("cleanurls-off", "vercel.json cleanUrls false",
     lambda d, sj: (d / "vercel.json").write_text('{"cleanUrls": false}', encoding="utf-8"),
     [("G3", "cleanUrls")], False, []),
    ("repo-null", "site.json repo_url null",
     lambda d, sj: sj.write_text(json.dumps({"name": "Filings Trail", "repo_url": None}), encoding="utf-8"),
     [("H", "repo_url not set")], False, []),
    ("repo-late", "repository link moved below the first <h2>",
     lambda d, sj: (edit(d / ADV, f' · <a href="{REPO_URL}">Repository</a>', ""),
                    edit(d / ADV, "</main>", f'<p><a href="{REPO_URL}">code</a></p></main>')) and None,
     [("H", "before the first <h2>")], False, []),
    ("pdf-link-late", "PDF link moved below the first <h2>",
     lambda d, sj: (edit(d / ADV, f'<a href="/adv/{PDF_NAME}">PDF</a> · ', ""),
                    edit(d / ADV, "</main>", f'<p><a href="/adv/{PDF_NAME}">PDF</a></p></main>')) and None,
     [("I1", "before the first <h2>")], False, []),
    ("pdf-truncated", "PDF truncated to half its bytes",
     lambda d, sj: (lambda p: p.write_bytes(p.read_bytes()[: p.stat().st_size // 2]))(d / "adv" / PDF_NAME),
     [("I2", "")], False, []),
    ("csv-rows", "drop one row from the CSV",
     lambda d, sj: (lambda p: p.write_text("\n".join(p.read_text(encoding="utf-8").splitlines()[:-1]) + "\n",
                                           encoding="utf-8"))(d / "adv/new_registrants.csv"),
     [("J", "1,692 data rows")], False, []),
    ("no-viewport+table-wrap", "drop the viewport meta and the table scroll wrapper",
     lambda d, sj: (edit(d / ADV, '<meta name="viewport" content="width=device-width, initial-scale=1">\n', ""),
                    edit(d / ADV, '<div class="table-scroll"><table>', "<div><table>", count=99)) and None,
     [("F1", "viewport"), ("F1", "NOT inside")], False, []),
    ("data-table-unwrapped", "the /data tables lose their scroll wrapper",
     lambda d, sj: edit(d / DATA, '<div class="table-scroll"><table>', "<div><table>", count=99),
     [("F1", "NOT inside")], False, []),
    ("wide-overflow", "a 600px-wide block outside any scroll container (measured at 320/375)",
     lambda d, sj: edit(d / ADV, "</main>", '<div style="width:600px">wide block</div></main>'),
     [("F2", "BODY SCROLLS HORIZONTALLY")], True, []),
    ("wide-overflow-data", "an unwrapped 1000px table on /data (measured at 320/375)",
     lambda d, sj: edit(d / DATA, '<div class="table-scroll"><table>', '<div><table style="width:1000px">'),
     [("F2", "data/index.html"), ("F1", "NOT inside")], True, []),
    ("pre-overflow-method", "the /method code block loses its scroll container (measured at 320/375)",
     lambda d, sj: edit(d / CSSF, "pre{overflow-x:auto;max-width:100%}", ""),
     [("F2", "method/index.html")], True, []),
    ("parser-mismatch", "malformed table: stray text foster-parented by the browser",
     lambda d, sj: edit(d / ROOT, "</main>", '<div class="table-scroll"><table><tr><td>cell</td></tr>'
                                             'STRAY TEXT</table></div></main>'),
     [("E4", "differs")], True, []),

    # ---- N: masthead, root work list, footers, print, sitemap
    ("nav-missing-link", "/method masthead nav loses the Data link",
     lambda d, sj: edit(d / METH, ' <a href="/data">Data</a></nav>', "</nav>"),
     [("N1", "nav links")], False, []),
    ("nav-order", "/data masthead nav in the order Work, Data, Method",
     lambda d, sj: edit(d / DATA, '<a href="/method">Method</a> <a href="/data" aria-current="page">Data</a>',
                        '<a href="/data" aria-current="page">Data</a> <a href="/method">Method</a>'),
     [("N1", "nav links")], False, []),
    ("aria-current-wrong", "/data: aria-current on Method instead of Data",
     lambda d, sj: edit(d / DATA, '<a href="/method">Method</a> <a href="/data" aria-current="page">Data</a>',
                        '<a href="/method" aria-current="page">Method</a> <a href="/data">Data</a>'),
     [("N1", "aria-current")], False, []),
    ("adv-aria-current", '/adv: Work aria-current="page" instead of "true"',
     lambda d, sj: edit(d / ADV, '<a href="/#work" aria-current="true">Work</a>', '<a href="/#work" aria-current="page">Work</a>'),
     [("N1", "aria-current")], False, []),
    ("root-h1-plain", "root: wordmark no longer the page's <h1>",
     lambda d, sj: edit(d / ROOT, '<h1 class="wordmark"><a href="/" aria-current="page">Filings Trail</a></h1>',
                        '<a class="wordmark" href="/" aria-current="page">Filings Trail</a>'),
     [("N1", "not inside the page's <h1>")], False, []),
    ("wordmark-h1-offroot", "/method: wordmark wrapped in an <h1>",
     lambda d, sj: edit(d / METH, '<a class="wordmark" href="/">Filings Trail</a>',
                        '<h1 class="wordmark"><a href="/">Filings Trail</a></h1>'),
     [("N1", "inside an <h1>")], False, []),
    ("old-site-header", "/adv: the old header.site home link comes back",
     lambda d, sj: edit(d / ADV, '<header class="masthead">',
                        '<header class="site"><nav><a href="/">Filings Trail</a></nav></header>\n<header class="masthead">'),
     [("N1", 'old <header class="site">')], False, []),
    ("404-no-masthead", "404 page without the masthead",
     lambda d, sj: resub(d / E404, r'<header class="masthead">.*?</header>', ""),
     [("N1", "404.html")], False, []),
    ("work-anchor-missing", 'root: the Work heading loses id="work"',
     lambda d, sj: edit(d / ROOT, '<h2 id="work">Work</h2>', "<h2>Work</h2>"),
     [("N1", '"#work" target'), ("G1", 'no id="work"')], False, []),
    ("in-progress-linked", "root: the Succession panel item becomes a link",
     lambda d, sj: edit(d / ROOT, "Succession panel <span", '<a href="/data">Succession panel</a> <span'),
     [("N2", "in-progress item is linked")], False, []),
    ("in-progress-label-missing", 'root: "Replication..." loses its "In progress" label',
     lambda d, sj: edit(d / ROOT, 'Replication on an earlier cohort <span class="status">In progress</span>',
                        "Replication on an earlier cohort"),
     [("N2", 'no "In progress" label')], False, []),
    ("in-progress-order", "root: the published work is no longer first",
     lambda d, sj: resub(d / ROOT, r'(<li><a href="/adv">.*?</li>)\n(<li class="in-progress">.*?</li>)', r"\2\n\1"),
     [("N2", "item 1")], False, []),
    ("root-copy-changed", "root: one word of the existing copy changes",
     lambda d, sj: edit(d / ROOT, "every figure in it is produced by code", "every figure in it is checked by code"),
     [("N2", "root copy changed")], False, []),
    ("footer-name", "/data footer names the author",
     lambda d, sj: edit(d / DATA, "Built from public SEC filings ·", "Built from public SEC filings by Ronan Schultz ·"),
     [("N3", "footer text"), ("C1", "appears 2 times")], False, []),
    ("footer-handle-visible", "/method footer link text shows the repo URL (owner handle visible)",
     lambda d, sj: edit(d / METH, f'<a href="{REPO_URL}">Code on GitHub</a>',
                        f'<a href="{REPO_URL}">github.com/ronan-schultz/filingstrail</a>'),
     [("C1", "stray"), ("N3", "footer")], False, []),
    ("handle-other-url", "/method links another URL under the owner handle (not the pinned repo)",
     lambda d, sj: edit(d / METH, "</main>", '<p><a href="https://github.com/ronan-schultz/other-repo">elsewhere</a></p></main>'),
     [("C1", "stray")], False, []),
    ("handle-sibling-repo", "/data <head> links a sibling repo whose name starts with the pinned one",
     lambda d, sj: edit(d / DATA, "</head>", '<link rel="me" href="https://github.com/ronan-schultz/filingstrail-notes">\n</head>'),
     [("C1", "stray")], False, []),
    ("handle-in-title", "/data: owner handle in a title attribute",
     lambda d, sj: edit(d / DATA, '<p class="note">', '<p class="note" title="ronan-schultz">'),
     [("C1", "stray")], False, []),
    ("footer-link-wrong", "/adv footer links a different repository",
     lambda d, sj: edit(d / ADV, f'<a href="{REPO_URL}">Code on GitHub</a>',
                        '<a href="https://github.com/example/other">Code on GitHub</a>'),
     [("N3", "footer links")], False, []),
    ("print-shows-masthead", "the @media print rule hiding masthead/footer is removed",
     lambda d, sj: edit(d / CSSF, PRINT_HIDE, ""),
     [("N4", "not hidden")], False, []),
    ("pdf-footer-text", "the footer line printed into the PDF",
     lambda d, sj: m_pdf_text(d, "Filings Trail - Built from public SEC filings - Code on GitHub"),
     [("N4", "PDF text")], False, []),
    ("pdf-nav-text", "the masthead nav printed into the PDF",
     lambda d, sj: m_pdf_text(d, "Filings Trail   Work   Method   Data"),
     [("N4", "masthead nav")], False, []),
    ("sitemap-missing-data", "sitemap.xml drops /data",
     lambda d, sj: edit(d / "sitemap.xml", "<url><loc>https://filingstrail.com/data</loc></url>\n", ""),
     [("N5", "sitemap.xml lists")], False, []),

    # ---- M: /method
    ("dropped-sentence-back", "the dropped METHOD.md sentence reappears on /method",
     lambda d, sj: edit(d / METH, "as a re-registration. The code now derives",
                        f"as a re-registration. {DROPPED} The code now derives"),
     [("M1", "An earlier version"), ("B", "1,573"), ("M2", "1,573")], False, []),
    ("dropped-sentence-comment", "the dropped sentence hidden in an HTML comment on /method",
     lambda d, sj: edit(d / METH, "</main>", "<!-- An earlier version of this analysis did that --></main>"),
     [("M1", "An earlier version")], False, []),
    ("limits-heading", 'the h3 reads "Limits - state these, don\'t hide them"',
     lambda d, sj: edit(d / METH, "<h3>Limits</h3>", "<h3>Limits — state these, don’t hide them</h3>"),
     [("M1", "state these")], False, []),
    ("h3-as-h2", '"Field mapping" rendered as an <h2>',
     lambda d, sj: edit(d / METH, "<h3>Field mapping</h3>", "<h2>Field mapping</h2>"),
     [("M1", "heading outline")], False, []),
    ("intro-edited", "one phrase of the /method intro changes on the page",
     lambda d, sj: edit(d / METH, "Every figure traces to a line of code", "Every figure traces to a script"),
     [("M1", "intro not rendered verbatim")], False, []),
    ("intro-link-wrong", "the intro's Data link points to /adv",
     lambda d, sj: edit(d / METH, '<a href="/data">Data</a> page', '<a href="/adv">Data</a> page'),
     [("M1", 'intro link "Data"')], False, []),
    ("intro-md-drift", "content/method-intro.md edited (page unchanged)",
     lambda d, sj: edit(d.parent / "content" / PAGES["method"]["intro_md"], "Every figure traces", "Each figure traces"),
     [("M1", "differs from the copy pinned")], False, []),
    ("extraction-altered", 'the extraction says "The window is thus" (not verbatim)',
     lambda d, sj: edit(d / METH, "The window is therefore", "The window is thus"),
     [("M1", "is not verbatim")], False, []),
    ("required-sentence", '"misclassifies" -> "reclassifies" in the required sentence',
     lambda d, sj: edit(d / METH, "misclassifies every August 2025", "reclassifies every August 2025"),
     [("M1", 'missing "Cutting the validation')], False, []),
    ("numeric-col-left", "the band table's numeric columns lose right alignment",
     lambda d, sj: edit(d / METH, ' style="text-align: right;"', "", count=999),
     [("M1", "not right-aligned")], False, []),
    ("table-flattened", "the Results table replaced by a paragraph (not a real table)",
     lambda d, sj: resub(d / METH, r'<div class="table-scroll"><table>\s*<thead>\s*<tr>\s*<th>Metric</th>.*?</table></div>',
                         "<p>Metric Value New registrants 1,693</p>"),
     [("M1", "<table> elements")], False, []),
    ("orphan-number", '"6,604 ERA rows" -> "6,640 ERA rows" on /method',
     lambda d, sj: edit(d / METH, "6,604 ERA rows", "6,640 ERA rows"),
     [("M2", "6,640")], False, []),
    ("orphan-pair", 'churn "+207/−76" -> "+207/−67" on /method',
     lambda d, sj: edit(d / METH, "+207/−76", "+207/−67"),
     [("M2", "+207/-67"), ("M3", "+207/-67")], False, []),
    ("orphan-in-title", "a number in the /method <title>",
     lambda d, sj: edit(d / METH, "<title>Method · Filings Trail</title>", "<title>Method · 57 files · Filings Trail</title>"),
     [("M2", '"57"'), ("M1", "<title>")], False, []),
    ("churn-log-mismatch", "report_stats.log section 8 says +207 / -75 (page unchanged)",
     lambda d, sj: m_log(d, "+207 / -76", "+207 / -75"),
     [("M3", "+207/-75")], False, []),
    ("churn-log-zero", "report_stats.log: the Aug->Sep 2025 transition is +1 / -0",
     lambda d, sj: m_log(d, "+0 / -0", "+1 / -0"),
     [("M3", "+0/-0")], False, []),

    # ---- D: /data
    ("data-row-dropped", "/data: one exempt row missing",
     lambda d, sj: resub(d / DATA, r'<tr><td><a href="[^"]*">ia010523-exempt\.zip</a>.*?</tr>\n', ""),
     [("D1", "rows, MANIFEST has")], False, []),
    ("data-row-extra", "/data: a row for a file that is not in the manifest",
     lambda d, sj: edit(d / DATA, "</tbody>", '<tr><td><a href="https://www.sec.gov/x/ia01012020.zip">ia01012020.zip</a></td>'
                                             '<td>2020-01-01</td><td>2019-12-31</td><td class="num">1</td><td>index</td>'
                                             '<td><code title="' + "0" * 64 + '">000000000000</code></td></tr></tbody>'),
     [("D1", "rows not in MANIFEST")], False, []),
    ("data-sha-prefix", "/data: one sha256 prefix off by a character",
     lambda d, sj: edit(d / DATA, f">{SHA_0402[:12]}<", f">{SHA_0402[:11]}0<"),
     [("D1", "sha256 cell")], False, []),
    ("data-sha-title", "/data: full sha256 dropped from the title attribute",
     lambda d, sj: edit(d / DATA, f' title="{SHA_0402}"', ""),
     [("D1", "title attribute")], False, []),
    ("data-rows-value", "/data: Rows 15,531 -> 15,532 for ia040224.zip",
     lambda d, sj: edit(d / DATA, ">15,531<", ">15,532<"),
     [("D1", "rows '15,532'")], False, []),
    ("data-source-label", '/data: a backfilled file labelled "index"',
     lambda d, sj: resub(d / DATA, r'(>ia040224\.zip</a></td>.*?<td class="num">15,531</td><td>)delisted(</td>)', r"\1index\2"),
     [("D1", "source 'index'")], False, []),
    ("data-order", "/data: two registered rows swapped (not newest first)",
     lambda d, sj: resub(d / DATA, r'(<tr><td><a href="[^"]*">ia08012025\.xlsx</a>[^\n]*</tr>)\n'
                                   r'(<tr><td><a href="[^"]*">ia07012025\.xlsx</a>[^\n]*</tr>)', r"\2\n\1"),
     [("D1", "not sorted")], False, []),
    ("data-link-wrong", "/data: a file name links the wrong URL",
     lambda d, sj: resub(d / DATA, r'href="[^"]*ia040224\.zip">ia040224\.zip</a>',
                         'href="https://www.sec.gov/wrong/ia040224.zip">ia040224.zip</a>'),
     [("D1", "link 'https://www.sec.gov/wrong")], False, []),
    ("data-intro-edited", "/data intro: one phrase changes on the page",
     lambda d, sj: edit(d / DATA, "41 were still there", "all were still there"),
     [("D1", "intro not rendered verbatim")], False, []),
    ("data-title", "/data <title> changed",
     lambda d, sj: edit(d / DATA, "<title>Data · Filings Trail</title>", "<title>Data</title>"),
     [("D1", "<title>")], False, []),
    ("panel-adv-mismatch", "/adv Sources says 16,369 rows for ia09022025.xlsx (/data unchanged)",
     lambda d, sj: edit(d / ADV, "<td>16,359</td>", "<td>16,369</td>"),
     [("D2", "rows disagrees")], False, []),
    ("panel-sha-adv", "/adv Sources sha256 of ia09022025.xlsx off by one hex digit",
     lambda d, sj: edit(d / ADV, SHA_PRIOR, SHA_PRIOR[:-1] + "d"),
     [("D2", "sha256 disagrees")], False, []),
    ("panel-data-rows", "/data Rows 17,149 -> 17,194 for the current panel file",
     lambda d, sj: edit(d / DATA, ">17,149<", ">17,194<"),
     [("D1", "rows '17,194'"), ("D2", "rows disagrees")], False, []),
    ("panel-class-missing", "/data: a panel row loses class=panel-file",
     lambda d, sj: edit(d / DATA, '<tr class="panel-file">', "<tr>"),
     [("D2", 'class="panel-file"')], False, []),
    ("panel-not-bold", "/data: a panel file name is not bold",
     lambda d, sj: edit(d / DATA, "<strong>ia09022025.xlsx</strong>", "ia09022025.xlsx"),
     [("D2", "<strong>")], False, []),
    ("dagger-missing", "/data: the dagger on ia09022025.xlsx is dropped",
     lambda d, sj: edit(d / DATA, "2025-07-31 †", "2025-07-31"),
     [("D3", "missing ['ia09022025.xlsx']")], False, []),
    ("dagger-extra", "/data: a dagger on ia08012025.xlsx (not stale)",
     lambda d, sj: resub(d / DATA, r'(>ia08012025\.xlsx</a></td><td>2025-08-01</td><td>2025-07-31)(</td>)', r"\1 †\2"),
     [("D3", "extra ['ia08012025.xlsx']")], False, []),
    ("dagger-note-missing", "/data: the dagger note is gone",
     lambda d, sj: edit(d / DATA, f'<p class="note">{PAGES["data"]["note"]}</p>', ""),
     [("D3", "note")], False, []),
    ("manifest-modified", "dist/data/manifest.csv: one byte changed",
     lambda d, sj: m_manifest_byte(d),
     [("D4", "!=")], False, []),
    ("manifest-missing", "dist/data/manifest.csv deleted",
     lambda d, sj: (d / "data" / "manifest.csv").unlink(),
     [("D4", "missing"), ("G1", "does not resolve")], False, []),

    # ---- K2 / H / H2
    ("halo-missing", "the .chart text halo rule is removed",
     lambda d, sj: edit(d / CSSF, HALO, ""),
     [("K2", "no stylesheet rule")], False, []),
    ("halo-fat", "the halo stroke-width is 8px",
     lambda d, sj: edit(d / CSSF, "stroke-width:3px", "stroke-width:8px"),
     [("K2", "stroke-width")], False, []),
    # Print may drop the halo (it bloats the PDF and doubles the text layer), but then
    # every label that sits on a filled shape in the PDF must clear the contrast floor.
    ("halo-print-off-illegible", "print drops the halo and the PDF has a #444 label on a #7a8aa0 bar",
     lambda d, sj: m_print_label_on_bar(d),
     [("K2", "without the halo")], False, []),
    # Screen may not drop it, in either theme.
    ("halo-screen-off", "an @media screen rule switches the halo off (stroke:none)",
     lambda d, sj: edit(d / CSSF, PRINT_HIDE, PRINT_HIDE + "\n@media screen{.chart text{stroke:none}}"),
     [("K2", "switched off inside @media screen")], False, []),
    ("halo-dark-off", "a dark-theme rule sets the halo stroke-width to 0",
     lambda d, sj: edit(d / CSSF, PRINT_HIDE, PRINT_HIDE + "\n@media (prefers-color-scheme: dark){.chart text{stroke-width:0}}"),
     [("K2", "switched off inside @media (prefers-color-scheme: dark)")], False, []),
    ("halo-screen-important", "a lower-specificity screen rule wins with !important",
     lambda d, sj: edit(d / CSSF, PRINT_HIDE, PRINT_HIDE + "\n@media screen{svg text{paint-order:normal!important}}"),
     [("K2", "switched off inside @media screen")], False, []),
    ("repo-url-other", "site.json repo_url points at another repository",
     lambda d, sj: sj.write_text(json.dumps({"name": "Filings Trail", "repo_url": "https://github.com/someone/else"}),
                                 encoding="utf-8"),
     [("H", "pins")], False, []),
    ("repo-link-branch", "/adv links the repository on branch 'main'",
     lambda d, sj: edit(d / ADV, "</main>", f'<p><a href="{REPO_URL}/blob/main/adv-report/report_stats.py">code</a></p></main>'),
     [("H2", "branch 'main'")], False, []),
    ("code-span-unlinked", "/method: the archive/fetch_adv.py code span is no longer linked",
     lambda d, sj: edit(d / METH, f'<a href="{REPO_URL}/blob/master/archive/fetch_adv.py"><code>archive/fetch_adv.py</code></a>',
                        "<code>archive/fetch_adv.py</code>"),
     [("H2", "names archive/fetch_adv.py but links nowhere")], False, []),
    ("repo-link-nofile", "/adv links a repository file that does not exist",
     lambda d, sj: edit(d / ADV, "</main>", f'<p><a href="{REPO_URL}/blob/master/adv-report/nope.py">code</a></p></main>'),
     [("H2", "not a file")], False, []),
]


def run_verify(dist: Path, site_json: Path, out: Path, tmp: Path, extra: list[str]) -> tuple[int, str]:
    cmd = [sys.executable, "-B", str(SITE / "verify.py"), "--dist", str(dist), "--site-json", str(site_json),
           "--out", str(out), "--tmp", str(tmp), "--content", str(dist.parent / "content"), *extra]
    p = subprocess.run(cmd, capture_output=True, timeout=900)
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def fail_lines(output: str) -> list[str]:
    lines = output.splitlines()
    res = []
    for i, ln in enumerate(lines):
        if ln.startswith("[FAIL]"):
            block = [ln]
            j = i + 1
            while j < len(lines) and lines[j].startswith("             "):
                block.append(lines[j].strip())
                j += 1
            res.append(" ".join(block))
    return res


def run_mutation(m, good: Path, work: Path, tmp: Path):
    name, desc, fn, expect, needs_browser, extra = m
    d = work / "mut" / name
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(good / "dist", d / "dist")
    shutil.copytree(good / "content", d / "content")
    msj = d / "site.json"
    shutil.copyfile(good / "site.json", msj)
    res = fn(d / "dist", msj)
    args = list(extra) + (res["args"] if isinstance(res, dict) else []) + ([] if needs_browser else ["--no-browser"])
    rc, out = run_verify(d / "dist", msj, d / "out", tmp, args)
    (d / "output.txt").write_text(out, encoding="utf-8")
    fl = fail_lines(out)
    missing = [(cid, sub) for cid, sub in expect
               if not any(re.match(rf"\[FAIL\] {re.escape(cid)}\s", x) and sub in x for x in fl)]
    ok = rc == 1 and not missing
    ids = sorted({x.split()[1] for x in fl})
    return name, desc, ok, rc, ids, missing, fl


# ------------------------------------------------------------------ live-mode smoke test (in process, no network)

def live_smoke(good: Path, work: Path, mutate=None):
    """Run verify.run_live() against the fixture through a stubbed fetcher. Returns the Report."""
    dist = good / "dist"
    files = {p.relative_to(dist).as_posix(): p.read_bytes() for p in dist.rglob("*") if p.is_file()}
    if mutate:
        mutate(files)

    def fake_fetch(url, timeout=30):
        sp = urllib.parse.urlsplit(url)
        h = email.message.Message()
        if sp.scheme == "http" or (sp.hostname or "").startswith("www."):
            h["Location"] = "https://filingstrail.com/"
            return {"url": url, "status": 308, "headers": h, "wire": b"", "body": b"", "enc": "identity", "ms": 1,
                    "cookies": []}
        path = sp.path or "/"
        if path == SITECHK["analytics_path"]:
            status, body, ctype = 200, b"/* stub */", "application/javascript"
        else:
            rel = verify.resolve_path(path, files)
            if rel is None:
                status, body, ctype = 404, files["404.html"], "text/html; charset=utf-8"
            else:
                status, body = 200, files[rel]
                ctype = verify.CTYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
        h["Content-Type"] = ctype
        return {"url": url, "status": status, "headers": h, "wire": body, "body": body, "enc": "identity", "ms": 1,
                "cookies": []}

    saved = verify.live_fetch, verify.tls_info
    verify.live_fetch = fake_fetch
    verify.tls_info = lambda host: {"subject": f"CN={host}", "issuer": "stub", "notAfter": "stub", "days_left": 90,
                                    "san": [host], "tls": "TLSv1.3"}
    R = verify.Report()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            verify.run_live("https://filingstrail.com", verify.load_works(HERE), SITECHK, R, dist, work / "live-out",
                            verify.load_pages(HERE), verify.Sources(REPO, good / "content", STATS_LOG))
    finally:
        verify.live_fetch, verify.tls_info = saved
    return R


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "filingstrail-verify-selftest"))
    ap.add_argument("--quick", action="store_true", help="skip the browser-dependent mutations")
    ap.add_argument("--only", nargs="*", help="run only these mutations")
    ap.add_argument("--jobs", type=int, default=4, help="parallel no-browser mutation runs")
    ap.add_argument("--reuse", action="store_true", help="reuse an existing fixture in --work (skips the rebuild)")
    a = ap.parse_args()
    work = Path(a.work).resolve()
    tmp = work / "profiles"
    good = work / "good"
    tmp.mkdir(parents=True, exist_ok=True)
    if not (a.reuse and (good / "dist" / "adv" / PDF_NAME).exists()):
        print(f"building fixture in {good} ...", flush=True)
        build_fixture(good, tmp)
    (work / "live-out").mkdir(exist_ok=True)

    results = []
    t = time.time()
    rc, out = run_verify(good / "dist", good / "site.json", good / "out", tmp, [])
    (work / "baseline-output.txt").write_text(out, encoding="utf-8")
    base_ok = rc == 0
    summ = next((ln for ln in out.splitlines() if ln.startswith("SUMMARY")), "")
    print(f"BASELINE (known-good fixture, full run with browser): exit {rc} "
          f"{'PASS as expected' if base_ok else 'UNEXPECTED FAILURE'} ({time.time() - t:.0f}s)  {summ}")
    if not base_ok:
        print("\n".join(fail_lines(out)))

    muts = [m for m in MUTATIONS if (not a.only or m[0] in a.only) and not (a.quick and m[4])]
    names = [m[0] for m in MUTATIONS]
    assert len(names) == len(set(names)), "duplicate mutation names"
    quick_ones = [m for m in muts if not m[4]]
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for r in ex.map(lambda m: run_mutation(m, good, work, tmp), quick_ones):
            results.append(r)
            name, desc, ok, rc, ids, missing, fl = r
            print(f"{'OK ' if ok else 'BAD'} {name:<28} exit {rc}  FAIL ids {ids}"
                  + (f"  MISSING {missing}" if missing else ""), flush=True)
    for m in [m for m in muts if m[4]]:
        r = run_mutation(m, good, work, tmp)
        results.append(r)
        name, desc, ok, rc, ids, missing, fl = r
        print(f"{'OK ' if ok else 'BAD'} {name:<28} exit {rc}  FAIL ids {ids}"
              + (f"  MISSING {missing}" if missing else ""), flush=True)

    if not a.only or "repo-null" in a.only:
        # repo-null again with --allow-missing-repo: H must become a WARN, not a FAIL
        d = work / "mut" / "repo-null"
        if d.exists():
            rc, out = run_verify(d / "dist", d / "site.json", d / "out-allow", tmp, ["--no-browser", "--allow-missing-repo"])
            fl = fail_lines(out)
            h_fail = any(x.startswith("[FAIL] H ") for x in fl)
            h_warn = any(ln.startswith("[WARN] H") and "repo_url not set" in ln for ln in out.splitlines())
            ok = h_warn and not h_fail
            results.append(("repo-null --allow-missing-repo", "same, flag passed", ok, rc, [], [], fl))
            print(f"{'OK ' if ok else 'BAD'} {'repo-null --allow-missing':<28} exit {rc}  H is "
                  f"{'WARN' if h_warn else '?'}{', FAIL' if h_fail else ''} (exit 1 only because --no-browser skips E4/F2)")

    if not a.only:
        # M3 without the log: an optional SKIP - no FAIL, and it does not make the verdict INCOMPLETE
        d = work / "mut" / "_no-stats-log"
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(good / "dist", d / "dist")
        shutil.copytree(good / "content", d / "content")
        rc, out = run_verify(d / "dist", good / "site.json", d / "out", tmp,
                             ["--no-browser", "--stats-log", str(d / "absent.log")])
        fl = fail_lines(out)
        skip_m3 = any(ln.startswith("[SKIP] M3") and "does not block" in ln for ln in out.splitlines())
        R = verify.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            R.ok("X", "a pass")
            R.skip_optional("M3", "log missing")
            verdict_rc = verify.finish(R, d, "optional-skip.txt", time.time())
        ok = skip_m3 and not fl and verdict_rc == 0
        results.append(("no-stats-log", "M3 without report_stats.log", ok, rc, [], [], fl))
        print(f"{'OK ' if ok else 'BAD'} {'no-stats-log':<28} exit {rc}  M3 SKIP optional={skip_m3}, FAILs {len(fl)}, "
              f"a report whose only SKIP is optional exits {verdict_rc}")

        # live mode: N, M, D run on the served bytes (stubbed fetcher, no network)
        R = live_smoke(good, work)
        want = ["N1", "N2", "N3", "N4", "N5", "M1", "M2", "M3", "D1", "D2", "D3", "D4"]
        seen = {c for s, c, _ in R.rows}
        bad = [(s, c, verify.ascii_ctx(m, 160)) for s, c, m in R.rows if s == "FAIL"]
        ok = not bad and all(c in seen for c in want) and \
            all(any(s == "PASS" and c == w for s, c, _ in R.rows) for w in want)
        results.append(("live-smoke", "run_live on the good fixture: N/M/D PASS on live bytes", ok, 0,
                        sorted(seen & set(want)), [], [f"[{s}] {c} {m}" for s, c, m in bad]))
        print(f"{'OK ' if ok else 'BAD'} {'live-smoke':<28} checks run on live bytes: {sorted(seen & set(want))}; "
              f"FAILs {bad[:3]}")
        lines_ran = sum(1 for s, c, _ in R.rows if c in want)

        def kill_dagger(files):
            files["data/index.html"] = files["data/index.html"].replace("2025-07-31 †".encode(), b"2025-07-31")
            files["method/index.html"] = files["method/index.html"].replace(b"6,604 ERA rows", b"6,640 ERA rows")
        R = live_smoke(good, work, kill_dagger)
        fl = [f"[{s}] {c} {m}" for s, c, m in R.rows if s == "FAIL"]
        orphan = any("orphan number" in ln and "6,640" in ln for ln in R.lines)   # evidence sits in the detail lines
        ok = any(x.startswith("[FAIL] D3") for x in fl) and any(x.startswith("[FAIL] M2") for x in fl) and orphan
        results.append(("live-smoke-defect", "live bytes: dagger dropped on /data, 6,640 on /method", ok, 1,
                        sorted({x.split()[1] for x in fl}), [], fl))
        print(f"{'OK ' if ok else 'BAD'} {'live-smoke-defect':<28} FAIL ids {sorted({x.split()[1] for x in fl})} "
              f"({lines_ran} N/M/D result lines on the good live run)")

    lines = [f"verify.py self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}  work dir {work}",
             f"BASELINE known-good fixture: {'PASS (exit 0)' if base_ok else 'FAILED'}  {summ}", ""]
    for name, desc, ok, rc, ids, missing, fl in results:
        lines.append(f"{'OK ' if ok else 'BAD'} {name:<30} {desc}")
        lines.append(f"      exit {rc}; FAIL ids {ids}" + (f"; MISSING {missing}" if missing else ""))
        for x in fl[:4]:
            lines.append("      " + verify.ascii_ctx(x, 230))
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out" / "selftest-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    bad = [r for r in results if not r[2]]
    print(f"\nself-test: baseline {'PASS' if base_ok else 'FAIL'}; {len(results) - len(bad)}/{len(results)} "
          f"mutations caught. Report: {HERE / 'out' / 'selftest-report.txt'}")
    return 0 if base_ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
