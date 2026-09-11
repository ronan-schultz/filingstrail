#!/usr/bin/env python3
"""Build filingstrail.com into dist/.

    python build.py                  # normal build
    python build.py --assets DIR     # resolve assets/ under DIR instead (testing only)

Reads site.json and renders:

    dist/index.html            root page (templates/root.html + the works list)
    dist/<slug>/index.html     one page per published work: its REPORT.md, rendered
    dist/<slug>/<csv>          the work's firm-level CSV, copied byte for byte
    dist/method/index.html     content/method-intro.md + each panel's METHOD.md appendix
    dist/data/index.html       content/data-intro.md + archive/MANIFEST.csv as tables
    dist/data/manifest.csv     archive/MANIFEST.csv, copied byte for byte
    dist/404.html, robots.txt, sitemap.xml, vercel.json

Everything in dist/ is deleted and regenerated except a .vercel folder. The PDF
is not made here; run pdf.py after this. Output is deterministic: the same
inputs give the same bytes.

The build fails (exit 1) rather than guess: an image with no SVG mapped in
site.json, a report title that disagrees with site.json, an appendix whose
start/end markers, dropped sentences or renamed headings are not found exactly
once, a phrase link whose text is not found exactly once, a manifest row it
cannot classify, a stale-file set that differs from site.json's expectation,
a GitHub link that is not into the configured repository, duplicate ids,
unbalanced tags, or any script other than the one analytics tag.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import html
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as etree

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from markdown.util import AtomicString

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DIST = HERE / "dist"
KEEP_IN_DIST = {".vercel"}

# smarty writes these characters rather than named entities, so the HTML source
# reads the same as the rendered page.
SMARTY = {"substitutions": {
    "left-single-quote": "‘", "right-single-quote": "’",
    "left-double-quote": "“", "right-double-quote": "”",
    "left-angle-quote": "«", "right-angle-quote": "»",
    "ndash": "–", "mdash": "—", "ellipsis": "…",
}}

HEX_RE = re.compile(r"[0-9a-f]{40,}")
NUM_RE = re.compile(r"[\s$<>+~%.,–—\-0-9]*[0-9][\s$<>+~%.,–—\-0-9]*")
FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*")
STASH_RE = re.compile("\x02wzxhzdk:(\\d+)\x03")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA_RE = re.compile(r"[0-9a-f]{64}")

PUBLISHED, IN_PROGRESS = "published", "in_progress"


class BuildError(Exception):
    pass


def fail(msg: str):
    raise BuildError(msg)


# ------------------------------------------------------------------ utilities
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(s.encode("utf-8"))           # "\n" line endings on every OS


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def entities(s: str) -> str:
    """Every character as a numeric reference: renders and works, no JS."""
    return "".join(f"&#{ord(c)};" for c in s)


def fill(template: str, **values: str) -> str:
    def sub(m):
        key = m.group(1)
        if key not in values:
            fail(f"template placeholder {{{{{key}}}}} has no value")
        return values[key]
    return re.sub(r"\{\{(\w+)\}\}", sub, template)


def minify_css(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};:,>])\s*", r"\1", css)
    return css.replace(";}", "}").strip()


def favicon_uri(svg: str) -> str:
    svg = re.sub(r">\s+<", "><", svg.strip())
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="/:=';,()@.-_ ").replace(" ", "%20")


def inline_md(text: str) -> str:
    """One line of markdown to inline HTML, with the same typography as pages."""
    out = markdown.markdown(text, extensions=["smarty"], output_format="html",
                            extension_configs={"smarty": SMARTY})
    m = re.fullmatch(r"<p>(.*)</p>", out.strip(), flags=re.S)
    if not m:
        fail(f"expected one inline paragraph from {text!r}")
    return m.group(1)


def tracked_files() -> set[str] | None:
    """Paths git tracks in this repo, or None when git is unavailable."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {p for p in out.decode("utf-8").split("\0") if p}


def status_of(w: dict) -> str:
    s = w.get("status", PUBLISHED)
    if s not in (PUBLISHED, IN_PROGRESS):
        fail(f"work {w.get('title')!r}: status must be {PUBLISHED!r} or {IN_PROGRESS!r}, got {s!r}")
    return s


# ------------------------------------------------------------------ chart SVGs
def chart_title(svg: str, where: str) -> str:
    """The chart's own headline: the first <text> set in the largest font size."""
    best, size = None, 0.0
    for m in re.finditer(r"<text\b([^>]*)>(.*?)</text>", svg, re.S):
        fs = re.search(r"font-size:\s*([\d.]+)px", m[1])
        text = " ".join(strip_tags(m[2]).split())
        if fs and text and float(fs[1]) > size:
            best, size = text, float(fs[1])
    if not best:
        fail(f"{where}: no <text> with a font-size to take the chart's title from")
    return best


def prepare_svg(text: str, prefix: str, describedby: str, taken: set[str]) -> tuple[str, str]:
    """Make a chart SVG safe to inline next to others on one page.

    Strips any XML prolog, names the root <svg> by the chart's own headline and
    describes it by its figcaption (so assistive tech hears the title once and
    the caption once, as the description). charts.py already prefixes ids per
    chart; if this SVG's ids still collide with a figure inlined earlier on the
    page (two matplotlib SVGs share "axes_1" and would cross-wire clip paths),
    they are namespaced with `prefix` here. `taken` collects the ids used so far
    on the page. Returns (svg, title).
    """
    s = text.replace("\r\n", "\n").strip()
    s = re.sub(r"^<\?xml[^>]*\?>\s*", "", s)
    s = re.sub(r"^<!DOCTYPE[^>]*>\s*", "", s, flags=re.I)
    s = re.sub(r"^(?:<!--.*?-->\s*)+", "", s, flags=re.S)
    if not s.startswith("<svg") or not s.endswith("</svg>"):
        fail(f"{prefix}: SVG must be a single <svg>...</svg> element")
    ids = set(re.findall(r'\sid="([^"]+)"', s))
    if ids & taken:
        s = re.sub(r'(\sid=")([^"]+)(")', lambda m: f"{m[1]}{prefix}-{m[2]}{m[3]}", s)
        s = re.sub(r"""url\(\s*(['"]?)#([^'")\s]+)\1\s*\)""",
                   lambda m: f"url(#{prefix}-{m[2]})" if m[2] in ids else m[0], s)
        s = re.sub(r'((?:xlink:)?href=")#([^"]+)(")',
                   lambda m: f"{m[1]}#{prefix}-{m[2]}{m[3]}" if m[2] in ids else m[0], s)
        ids = {f"{prefix}-{i}" for i in ids}
    taken |= ids
    title = chart_title(s, prefix)
    m = re.match(r"<svg\b([^>]*?)(/?)>", s)
    attrs = re.sub(r'\s(?:role|aria-labelledby|aria-describedby|aria-label|aria-hidden)="[^"]*"', "", m[1])
    return (f'<svg{attrs} role="img" aria-label="{esc(title)}" aria-describedby="{describedby}"{m[2]}>'
            + s[m.end():], title)


# ------------------------------------------------------------------ markdown
class Ctx:
    """What one Markdown source's rendering needs to know."""

    def __init__(self, site: dict, source: Path, label: str, asset_root: Path,
                 tracked: set[str] | None, figures: dict | None = None):
        self.source = source
        self.label = label                      # how errors name this source
        self.figures = figures or {}
        self.asset_root = asset_root
        self.source_dir = source.resolve().parent
        self.repo_url = site["repo_url"]
        self.branch = site["repo_branch"]
        self.tracked = tracked
        self.figures_used: list[str] = []
        self.svg_ids: set[str] = set()
        self.code_linked: list[str] = []
        self.code_linkable: list[str] = []

    def repo_file(self, name: str) -> str | None:
        """Repo-relative path if `name` is a tracked file, looked up first in the
        source's own directory and then from the repository root (so both
        `METHOD.md` in adv-report/ and `archive/fetch_adv.py` resolve). The first
        location where the file exists decides; an untracked file is not linked."""
        if not FILE_RE.fullmatch(name) or ".." in name.split("/"):
            return None
        for base in (self.source_dir, REPO):
            p = base / name
            if not p.is_file():
                continue
            try:
                rel = p.resolve().relative_to(REPO).as_posix()
            except ValueError:
                return None
            if self.tracked is not None and rel not in self.tracked:
                return None
            return rel
        return None


class SiteTreeprocessor(Treeprocessor):
    """Figures, table wrappers and alignment, code-span links, focusable code blocks."""

    def __init__(self, md, ctx: Ctx):
        super().__init__(md)
        self.ctx = ctx

    def text(self, el) -> str:
        t = "".join(el.itertext())
        blocks = self.md.htmlStash.rawHtmlBlocks
        return STASH_RE.sub(lambda m: strip_tags(blocks[int(m[1])]), t).strip()

    def run(self, root):
        parents = {c: p for p in root.iter() for c in p}
        self.figures(root, parents)
        self.tables(root, parents)
        self.code(root, parents)
        for pre in root.iter("pre"):
            pre.set("tabindex", "0")             # it scrolls sideways on phones

    @staticmethod
    def replace(parents, old, new):
        parent = parents[old]
        i = list(parent).index(old)
        parent.remove(old)
        parent.insert(i, new)
        new.tail, old.tail = old.tail, None
        parents[new] = parent

    def figures(self, root, parents):
        for img in list(root.iter("img")):
            src, alt = img.get("src", ""), img.get("alt", "")
            p = parents[img]
            if p.tag != "p" or len(p) != 1 or (p.text or "").strip() or (img.tail or "").strip():
                fail(f"image {src!r} must be a paragraph of its own")
            rel = self.ctx.figures.get(src)
            if not rel:
                fail(f"image {src!r} in {self.ctx.label} has no SVG mapped in site.json "
                     f"(figures of the entry that renders it)")
            path = self.ctx.asset_root / rel
            if not path.is_file():
                fail(f"missing chart asset {path} (run charts.py)")
            if not alt.strip():
                fail(f"image {src!r} has no alt text to use as its caption")
            stem = Path(src).stem
            cap_id = f"fig-{stem}"
            svg, title = prepare_svg(read_text(path), stem, cap_id, self.ctx.svg_ids)
            fig = etree.Element("figure")
            # The scroll box is a named region (it takes focus for keyboard
            # scrolling); its name is the chart's short title, not the caption.
            box = etree.SubElement(fig, "div", {"class": "fig-scroll", "role": "region",
                                                "aria-label": title, "tabindex": "0"})
            box.text = self.md.htmlStash.store(svg)
            cap = etree.SubElement(fig, "figcaption", {"id": cap_id})
            cap.text = alt
            self.replace(parents, p, fig)
            self.ctx.figures_used.append(src)

    def tables(self, root, parents):
        for table in list(root.iter("table")):
            thead, tbody = table.find("thead"), table.find("tbody")
            if thead is None or tbody is None:
                fail("every table needs a header row")
            head = thead.find("tr").findall("th")
            rows = [tr.findall("td") for tr in tbody.findall("tr")]
            # Alignment is decided per column, not per cell, so one bold or
            # en-dashed value cannot knock itself out of line. Column 0 is the
            # label column and always reads left.
            for c in range(len(head)):
                cells = [head[c]] + [r[c] for r in rows if c < len(r)]
                styles = {x.attrib.pop("style", "") for x in cells}
                vals = [self.text(r[c]) for r in rows if c < len(r)]
                numeric = c > 0 and bool(vals) and sum(bool(NUM_RE.fullmatch(v)) for v in vals) / len(vals) >= 0.6
                if any("right" in s for s in styles):
                    numeric = True
                if numeric:
                    for x in cells:
                        x.set("class", "num")
            label = "Table: " + ", ".join(self.text(h) for h in head)
            # A column of checksums never fits a phone: that table runs to the
            # screen edge there (style.css .table-wrap.bleed) so the cut shows.
            wide = any(HEX_RE.fullmatch(self.text(c)) for r in rows for c in r)
            wrap = etree.Element("div", {"class": "table-wrap bleed" if wide else "table-wrap",
                                         "role": "region", "aria-label": label, "tabindex": "0"})
            self.replace(parents, table, wrap)
            wrap.append(table)

    @staticmethod
    def no_hyphen_breaks(code) -> None:
        """Inline code may wrap at its spaces but never at a hyphen inside a token:
        `--prior` must not print as "-" / "-prior", nor a file name split at its
        dash. A hyphenated single token gets class nobr; in a multi-token span each
        hyphenated token is wrapped in <span class="nobr">. The text is unchanged."""
        text = code.text or ""
        if len(code) or "-" not in text:
            return
        tokens = re.split(r"(\s+)", text)
        if len(tokens) == 1:
            code.set("class", "nobr")
            return
        # AtomicString throughout: smarty's own inline pass runs after this one
        # (priority 6) and would otherwise turn "--current" into "–current".
        head, last = "", None
        for tok in tokens:
            if "-" in tok and not tok.isspace():
                if last is None:
                    code.text = AtomicString(head)
                last = etree.SubElement(code, "span", {"class": "nobr"})
                last.text = AtomicString(tok)
            elif last is None:
                head += tok
            else:
                last.tail = AtomicString((last.tail or "") + tok)

    def code(self, root, parents):
        for code in list(root.iter("code")):
            name = (code.text or "").strip()
            anc, nested, in_pre = parents.get(code), False, False
            while anc is not None:
                nested = nested or anc.tag in ("a", "pre")
                in_pre = in_pre or anc.tag == "pre"
                anc = parents.get(anc)
            if HEX_RE.fullmatch(name):
                code.set("class", "hash")          # checksums may wrap anywhere
            elif not in_pre:
                self.no_hyphen_breaks(code)
            rel = None if nested else self.ctx.repo_file(name)
            if not rel:
                continue
            self.ctx.code_linkable.append(name)
            if self.ctx.repo_url:
                a = etree.Element("a", {"href": f"{self.ctx.repo_url}/blob/{self.ctx.branch}/{rel}"})
                self.replace(parents, code, a)
                a.append(code)
                self.ctx.code_linked.append(name)


class SiteExtension(Extension):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx

    def extendMarkdown(self, md):
        # After inline parsing (20), before prettify (10) and toc (5).
        md.treeprocessors.register(SiteTreeprocessor(md, self.ctx), "filingstrail", 15)


# A date or a numeric range written with a hyphen or en dash ("2025-08-01", "2-5",
# "$25–100M"): one unit that must not break across lines at its dash.
NUM_RUN_RE = re.compile(r"(?<![\w$.,])\$?\d+(?:[.,]\d+)*(?:[-–]\$?\d+(?:[.,]\d+)*[A-Za-z%]*)+(?![\w-])")


def nobr_numbers(html_text: str) -> str:
    """Wrap dates and numeric ranges in prose in <span class="nobr">. Text inside
    tags, <code>, <pre>, <svg>, <style> and <script> is left alone; the page's
    text is unchanged, only where a line may break."""
    parts = re.split(r"(<[^>]+>)", html_text)
    skip = 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            tag = re.match(r"</?\s*([A-Za-z0-9]+)", part)
            name = tag[1].lower() if tag else ""
            if name in ("code", "pre", "svg", "style", "script") and not part.endswith("/>"):
                skip += -1 if part.startswith("</") else 1
            continue
        if not skip and part:
            parts[i] = NUM_RUN_RE.sub(lambda m: f'<span class="nobr">{m[0]}</span>', part)
    return "".join(parts)


def render_md(text: str, ctx: Ctx) -> str:
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "smarty", SiteExtension(ctx)],
                           extension_configs={"smarty": SMARTY}, output_format="html")
    out = nobr_numbers(md.convert(text))
    # fenced_code stashes its blocks as raw HTML, out of the treeprocessor's
    # reach; they scroll sideways on phones, so make them focusable here too.
    out = out.replace("<pre>", '<pre tabindex="0">')

    # The serializer sorts attributes; put the scroll wrappers back in reading
    # order: <div class="table-wrap" role="region" aria-label="..." tabindex="0">.
    def order(m):
        attrs = re.findall(r'\s([\w:-]+)="([^"]*)"', m[1])
        rank = {"class": 0, "role": 1, "aria-label": 2, "aria-labelledby": 2, "tabindex": 3}
        attrs.sort(key=lambda kv: rank.get(kv[0], 4))
        return "<div" + "".join(f' {k}="{v}"' for k, v in attrs) + ">"
    return re.sub(r'<div((?:\s[\w:-]+="[^"]*")*\sclass="(?:table-wrap(?: bleed)?|fig-scroll)"(?:\s[\w:-]+="[^"]*")*)>',
                  order, out)


# ------------------------------------------------------------------ html surgery
H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S)


def section_span(body: str, title: str) -> tuple[int, int]:
    heads = list(H2_RE.finditer(body))
    found = [i for i, m in enumerate(heads) if strip_tags(m[1]) == title]
    if len(found) != 1:
        fail(f"expected exactly one section headed {title!r}, found {len(found)}")
    i = found[0]
    end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
    return heads[i].start(), end


def link_phrase(body: str, section: str, phrase: str, href: str, download: bool) -> str:
    """Wrap the one occurrence of `phrase` in `section` in a link; text unchanged."""
    s, e = section_span(body, section)
    parts = re.split(r"(<[^>]+>)", body[s:e])
    needle = html.escape(phrase, quote=False)
    hits, in_a, in_svg = [], 0, 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            tag = re.match(r"</?\s*([A-Za-z0-9]+)", part)
            name = tag[1].lower() if tag else ""
            step = -1 if part.startswith("</") else (0 if part.endswith("/>") else 1)
            if name == "a":
                in_a += step
            elif name == "svg":
                in_svg += step
            continue
        if not in_a and not in_svg:
            hits += [i] * part.count(needle)
    if len(hits) != 1:
        fail(f"expected {phrase!r} exactly once (outside links) in section {section!r}, "
             f"found {len(hits)}")
    attrs = f' href="{esc(href)}"' + (" download" if download else "")
    parts[hits[0]] = parts[hits[0]].replace(needle, f"<a{attrs}>{needle}</a>", 1)
    return body[:s] + "".join(parts) + body[e:]


def extract_block(path: Path, start: str, end: str, drops: list[str]) -> str:
    """Lines strictly between the `start` and `end` marker lines, minus `drops`."""
    lines = read_text(path).split("\n")
    si = [i for i, ln in enumerate(lines) if ln.strip() == start]
    ei = [i for i, ln in enumerate(lines) if ln.strip() == end]
    if len(si) != 1 or len(ei) != 1 or ei[0] <= si[0]:
        fail(f"{path.name}: need exactly one {start!r} line followed by exactly one {end!r} "
             f"line; found {len(si)} and {len(ei)}")
    block = lines[si[0] + 1:ei[0]]
    level = len(start) - len(start.lstrip("#"))
    for ln in block:
        m = re.match(r"(#+)\s", ln)
        if m and len(m[1]) <= level:
            fail(f"{path.name}: heading {ln!r} sits between {start!r} and {end!r}; "
                 f"the appendix would swallow another section")
    text = "\n".join(block).strip("\n") + "\n"
    for d in drops:
        n = text.count(d)
        if n != 1:
            fail(f"{path.name}: expected the sentence {d!r} exactly once between {start!r} "
                 f"and {end!r}, found {n}. The source changed; review the appendix.")
        for variant in (" " + d, d + " ", d):
            if variant in text:
                text = text.replace(variant, "", 1)
                break
    return text


# ------------------------------------------------------------------ method appendix
def extract_line(path: Path, prefix: str) -> str:
    """The one line of `path` that starts with `prefix`."""
    hits = [ln for ln in read_text(path).split("\n") if ln.startswith(prefix)]
    if len(hits) != 1:
        fail(f"{path.name}: expected exactly one line starting {prefix!r}, found {len(hits)}")
    return hits[0].rstrip()


def extract_tail(path: Path, start: str) -> str:
    """From the one `start` line (inclusive) to the end of the file."""
    lines = read_text(path).split("\n")
    si = [i for i, ln in enumerate(lines) if ln.strip() == start]
    if len(si) != 1:
        fail(f"{path.name}: expected exactly one {start!r} line, found {len(si)}")
    return "\n".join(lines[si[0]:]).strip("\n") + "\n"


def md_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Group lines into ('p', lines) runs of non-blank lines, ('code', lines) fenced
    blocks and ('blank', [line]) separators, in order."""
    blocks, cur, fence = [], [], False
    for ln in text.split("\n"):
        if FENCE_RE.match(ln):
            if not fence:
                if cur:
                    blocks.append(("p", cur))
                cur, fence = [ln], True
            else:
                blocks.append(("code", cur + [ln]))
                cur, fence = [], False
            continue
        if fence:
            cur.append(ln)
        elif ln.strip():
            cur.append(ln)
        else:
            if cur:
                blocks.append(("p", cur))
                cur = []
            blocks.append(("blank", [ln]))
    if fence:
        fail("unterminated code fence in an extracted Markdown block")
    if cur:
        blocks.append(("p", cur))
    return blocks


def set_headings(text: str, level: int, renames: dict[str, str], source: str) -> str:
    """Every heading outside code fences becomes `level`; `renames` maps heading
    text to replacement text, and each must match exactly one heading."""
    used: Counter = Counter()
    out = []
    for kind, lines in md_blocks(text):
        if kind == "p":
            new = []
            for ln in lines:
                m = HEAD_RE.match(ln)
                if m:
                    if len(m[1]) == 1:
                        fail(f"{source}: a top-level heading {ln!r} sits inside the extracted block")
                    title = m[2]
                    if title in renames:
                        used[title] += 1
                        title = renames[title]
                    ln = "#" * level + " " + title
                new.append(ln)
            lines = new
        out.extend(lines)
    for k in renames:
        if used[k] != 1:
            fail(f"{source}: expected exactly one heading {k!r} to rename, found {used[k]}. "
                 f"The source changed; review the method page.")
    return "\n".join(out)


def drop_sentence(text: str, sentence: str, source: str) -> str:
    """Remove `sentence` from the one prose paragraph that contains it, matching
    after collapsing the whitespace within each paragraph (hard wraps)."""
    want = " ".join(sentence.split())
    blocks = md_blocks(text)
    hits = []
    for i, (kind, lines) in enumerate(blocks):
        if kind == "p":
            hits += [i] * " ".join(" ".join(lines).split()).count(want)
    if len(hits) != 1:
        fail(f"{source}: expected the sentence {want!r} exactly once, found {len(hits)}. "
             f"The source changed; review the method page.")
    kind, lines = blocks[hits[0]]
    if any(re.match(r"\s*(?:[-*+>|#]|\d+[.)])(?:\s|$)", ln) for ln in lines):
        fail(f"{source}: the sentence to drop sits in a list, table, quote or heading, "
             f"not a plain paragraph; review the method page")
    flat = " ".join(" ".join(lines).split())
    for variant in (" " + want, want + " ", want):
        if variant in flat:
            flat = flat.replace(variant, "", 1)
            break
    if not flat.strip():
        fail(f"{source}: dropping {want!r} would leave an empty paragraph")
    blocks[hits[0]] = (kind, [flat])
    return "\n".join(ln for _, lines in blocks for ln in lines)


def method_panel_md(p: dict) -> str:
    src = HERE / p["from"]
    run = extract_line(src, p["run_line"])
    tail = extract_tail(src, p["start"])
    tail = set_headings(tail, int(p.get("heading_level", 3)), p.get("rename_headings", {}), p["from"])
    for d in p.get("drop", []):
        tail = drop_sentence(tail, d, p["from"])
    return run + "\n\n" + tail


# ------------------------------------------------------------------ data page
MANIFEST_COLS = ("filename", "vintage", "data_through", "variant_by_content", "n_rows",
                 "source", "sha256", "url")


def iso_date(s: str, what: str) -> str:
    if not DATE_RE.fullmatch(s or ""):
        fail(f"manifest: {what} is {s!r}, not YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        fail(f"manifest: {what} is {s!r}, not a calendar date")
    return s


def read_manifest(path: Path, variants: list[str], sources: dict) -> list[dict]:
    """archive/MANIFEST.csv rows, every field this page uses checked, never guessed."""
    if not path.is_file():
        fail(f"missing {path}")
    rows = list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))
    if not rows:
        fail(f"{path.name} has no rows")
    missing = [c for c in MANIFEST_COLS if c not in rows[0]]
    if missing:
        fail(f"{path.name}: missing columns {missing}")
    for i, r in enumerate(rows, 2):
        where = f"{path.name} line {i} ({r.get('filename')!r})"
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", r["filename"] or ""):
            fail(f"{where}: filename {r['filename']!r} is not a plain file name")
        iso_date(r["vintage"], f"{where} vintage")
        iso_date(r["data_through"], f"{where} data_through")
        if r["variant_by_content"] not in variants:
            fail(f"{where}: variant_by_content {r['variant_by_content']!r} is not one of {variants}")
        if r["source"] not in sources:
            fail(f"{where}: source {r['source']!r} is not one of {sorted(sources)}")
        if not re.fullmatch(r"\d+", r["n_rows"] or ""):
            fail(f"{where}: n_rows {r['n_rows']!r} is not a count")
        if not SHA_RE.fullmatch(r["sha256"] or ""):
            fail(f"{where}: sha256 {r['sha256']!r} is not 64 lowercase hex digits")
        u = r["url"] or ""
        if not (u.startswith("https://www.sec.gov/") and u.endswith("/" + r["filename"])):
            fail(f"{where}: url {u!r} is not the SEC's copy of this file")
    names = Counter(r["filename"] for r in rows)
    dupes = sorted(n for n, c in names.items() if c > 1)
    if dupes:
        fail(f"{path.name}: files listed more than once: {dupes}")
    return rows


def stale_files(rows: list[dict]) -> set[str]:
    """Files whose data does not advance: data_through no later than the latest
    data_through among earlier-vintage files of the same variant (the rule in
    archive/fetch_adv.py stale_against, taken against every earlier file)."""
    stale = set()
    for r in rows:
        earlier = [e["data_through"] for e in rows
                   if e["variant_by_content"] == r["variant_by_content"] and e["vintage"] < r["vintage"]]
        if earlier and r["data_through"] <= max(earlier):
            stale.add(r["filename"])
    return stale


# ------------------------------------------------------------------ checks
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


class PageCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.scripts: list[dict] = []
        self.script_text = ""
        self.srcs: list[str] = []
        self.hrefs: list[str] = []

    def _attrs(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if "src" in a and tag != "script":
            self.srcs.append(a["src"])
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag == "script":
            self.scripts.append(a)
        return a

    def handle_starttag(self, tag, attrs):
        self._attrs(tag, attrs)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._attrs(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {'/'.join(self.stack[-4:])})")
            return
        self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "script":
            self.script_text += data


def check_page(name: str, page: str, analytics_src: str) -> PageCheck:
    pc = PageCheck()
    pc.feed(page)
    pc.close()
    problems = list(pc.errors)
    if pc.stack:
        problems.append(f"unclosed tags: {pc.stack}")
    dupes = sorted({i for i in pc.ids if pc.ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate ids: {dupes}")
    if pc.scripts != [{"defer": None, "src": analytics_src}] or pc.script_text.strip():
        problems.append(f"expected exactly the analytics script and nothing else, got {pc.scripts}")
    if pc.srcs:
        problems.append(f"unexpected src attributes (no external requests): {pc.srcs}")
    if re.search(r"url\(\s*['\"]?(?!#)", page) or "@import" in page:
        problems.append("url()/@import found: no external requests allowed")
    if problems:
        fail(f"{name}: " + "; ".join(problems))
    return pc


def check_links(pages: dict[str, str], checks: dict[str, PageCheck], extra_files: set[str]) -> None:
    """Every root-absolute link resolves to a page or file this build writes
    (cleanUrls: /adv -> adv/index.html), and every #fragment has its id."""
    ids = {name: set(pc.ids) for name, pc in checks.items()}
    files = set(pages) | extra_files

    def target(path: str) -> str | None:
        if path == "/":
            return "index.html"
        p = path.lstrip("/")
        for cand in (p, p + ".html", p + "/index.html"):
            if cand in files:
                return cand
        return None

    for name, pc in checks.items():
        for href in pc.hrefs:
            if href.startswith(("http://", "https://", "mailto:", "&#")):
                continue
            if href.startswith("#"):
                if href[1:] not in ids[name]:
                    fail(f"{name}: link {href} has no target id on the page")
                continue
            if not href.startswith("/"):
                fail(f"{name}: relative link {href!r}; links must be root-absolute")
            path, _, frag = href.partition("#")
            t = target(path)
            if t is None:
                fail(f"{name}: link {href!r} does not resolve to anything in dist")
            if frag and (t not in ids or frag not in ids[t]):
                fail(f"{name}: link {href!r}: no id={frag!r} on {t}")


# ------------------------------------------------------------------ pages
class Site:
    def __init__(self, cfg: dict, asset_root: Path):
        self.cfg = cfg
        self.asset_root = asset_root
        self.base = cfg["base_url"].rstrip("/")
        repo = cfg.get("repo_url")
        if repo is not None:
            repo = repo.strip().rstrip("/")
            repo = repo[:-4] if repo.endswith(".git") else repo
            if not re.fullmatch(r"https://[^\s\"'<>]+", repo):
                fail(f"repo_url must be an https URL, got {cfg['repo_url']!r}")
        cfg["repo_url"] = repo
        self.css = minify_css(read_text(HERE / "static" / "style.css"))
        self.favicon = favicon_uri(read_text(HERE / "static" / "favicon.svg"))
        self.shell = read_text(HERE / "templates" / "page.html")
        self.tracked = tracked_files()
        self.pages: dict[str, str] = {}
        self.urls: list[str] = []                 # sitemap, in site order
        self.published = [w for w in cfg["works"] if status_of(w) == PUBLISHED]
        self.in_progress = [w for w in cfg["works"] if status_of(w) == IN_PROGRESS]
        for w in self.in_progress:
            extra = sorted(set(w) - {"title", "descriptor", "status"})
            if extra:
                fail(f"in-progress work {w['title']!r} has {extra}; an in-progress entry is a "
                     f"title, an optional descriptor and a status, no page")
        self.nav = cfg["nav"]
        hrefs = [n["href"] for n in self.nav]
        if len(set(hrefs)) != len(hrefs) or not all(h.startswith("/") for h in hrefs):
            fail(f"nav hrefs must be unique and root-absolute: {hrefs}")
        self.work_nav = f"/#{cfg['root']['works_id']}"

    # -------------------------------------------------------------- chrome
    def masthead(self, current: str | None = None, mode: str = "page", root: bool = False) -> str:
        """Wordmark and site nav. `current` is the nav href that gets aria-current=`mode`."""
        if current is not None and current not in [n["href"] for n in self.nav]:
            fail(f"page claims nav item {current!r}, which is not in site.json nav")
        name = esc(self.cfg["name"])
        mark = (f'<h1 class="wordmark"><a href="/" aria-current="page">{name}</a></h1>' if root
                else f'<a class="wordmark" href="/">{name}</a>')
        links = []
        for n in self.nav:
            cur = f' aria-current="{mode}"' if n["href"] == current else ""
            links.append(f'<a href="{esc(n["href"])}"{cur}>{esc(n["text"])}</a>')
        return ('<header class="masthead">\n' + mark + '\n<nav aria-label="Site">\n'
                + "\n".join(links) + "\n</nav>\n</header>")

    def footer(self) -> str:
        f = self.cfg["footer"]
        parts = [esc(self.cfg["name"]), esc(f["tagline"])]
        if self.cfg["repo_url"]:
            parts.append(f'<a href="{esc(self.cfg["repo_url"])}">{esc(f["repo_text"])}</a>')
        # A no-break space before each separator: when the line wraps, the dot
        # ends a line instead of starting the next one.
        return "<footer>\n<p>" + "&nbsp;· ".join(parts) + "</p>\n</footer>"

    def page(self, *, title, description, canonical, og_title, og_type, body, robots=""):
        """`canonical` None (the 404 page): no canonical link and no og:url, so a
        page that is not at one address does not claim the home page's."""
        link = f'<link rel="canonical" href="{esc(canonical)}">\n' if canonical else ""
        og_url = f'<meta property="og:url" content="{esc(canonical)}">\n' if canonical else ""
        return fill(self.shell, title=esc(title), description=esc(description),
                    canonical_link=link, og_url=og_url, og_title=esc(og_title), og_type=og_type,
                    site_name=esc(self.cfg["name"]), favicon=self.favicon, css=self.css,
                    analytics_src=esc(self.cfg["analytics_src"]), robots=robots,
                    body=body.strip("\n"))

    def check_repo_links(self, body: str, source: str) -> None:
        """GitHub links written into authored copy must point into repo_url, on
        repo_branch, at a file git tracks: a moved file or repository fails here."""
        repo, branch = self.cfg["repo_url"], self.cfg["repo_branch"]
        for href in re.findall(r'href="(https://github\.com/[^"]*)"', body):
            href = html.unescape(href)
            if not repo:
                print(f"  warning: {source} links {href} but repo_url is not set")
                continue
            if not href.startswith(repo + "/") and href != repo:
                fail(f"{source}: GitHub link {href} is not into repo_url {repo}")
            m = re.fullmatch(re.escape(repo) + r"/(?:blob|tree)/([^/]+)/(.+)", href)
            if m:
                if m[1] != branch:
                    fail(f"{source}: {href} is on branch {m[1]!r}, repo_branch is {branch!r}")
                if self.tracked is not None and m[2] not in self.tracked:
                    fail(f"{source}: {href} names {m[2]}, which git does not track")

    # -------------------------------------------------------------- root
    def root(self):
        c = self.cfg
        label = esc(c["root"]["in_progress_label"])
        items = []
        for w in c["works"]:
            if status_of(w) == PUBLISHED:
                items.append(f'<li><a href="/{w["slug"]}">{inline_md(w["title"])}</a> '
                             f'<span class="desc">{inline_md(w["descriptor"])}</span></li>')
            else:
                parts = [f'<span class="title">{inline_md(w["title"])}</span>',
                         f'<span class="label">{label}</span>']
                if w.get("descriptor"):
                    parts.append(f'<span class="desc">{inline_md(w["descriptor"])}</span>')
                items.append('<li class="in-progress">' + " ".join(parts) + "</li>")
        body = fill(read_text(HERE / c["root"]["template"]),
                    masthead=self.masthead(root=True),
                    works_id=esc(c["root"]["works_id"]),
                    works_heading=esc(c["root"]["works_heading"]), works="\n".join(items),
                    author=esc(c["author"]), email=entities(c["email"]),
                    mailto=entities("mailto:" + c["email"]))
        self.pages["index.html"] = self.page(
            title=c["name"], description=c["root"]["description"], canonical=self.base + "/",
            og_title=c["name"], og_type="website", body=body)
        self.urls.append(self.base + "/")

    # -------------------------------------------------------------- works
    def work(self, w: dict) -> dict:
        src = HERE / w["source_md"]
        ctx = Ctx(self.cfg, src, w["source_md"], self.asset_root, self.tracked, w["figures"])
        text = read_text(src)
        lines = text.split("\n")
        first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        m = re.fullmatch(r"# (.+)", lines[first].strip()) if first is not None else None
        if not m:
            fail(f"{src.name} must open with a '# ' title line")
        if m[1].strip() != w["title"]:
            fail(f"title mismatch: {src.name} says {m[1].strip()!r}, site.json says {w['title']!r}")
        title_html = inline_md(m[1].strip())
        title_text = strip_tags(title_html)
        body = render_md("\n".join(lines[first + 1:]), ctx)

        for img in set(w["figures"]) - set(ctx.figures_used):
            print(f"  warning: site.json maps {img} but {src.name} does not use it")

        for ap in w.get("appendix", []):
            block = extract_block(HERE / ap["from"], ap["start"], ap["end"], ap.get("drop", []))
            ap_html = render_md(block, ctx)
            note = inline_md_with_links(ap["note"], ctx) if ap.get("note") else ""
            slug = slugify(ap["heading"])
            section = (f'<h2 id="{slug}">{esc(ap["heading"])}</h2>\n{ap_html.strip()}\n'
                       + (f'<p class="note">{note}</p>\n' if note else ""))
            _, end = section_span(body, ap["after"])
            body = body[:end].rstrip("\n") + "\n" + section + body[end:]

        for ln in w.get("links", []):
            body = link_phrase(body, ln["section"], ln["text"], ln["href"], ln.get("download", False))

        repo = self.cfg["repo_url"]
        pdf_href = f'/{w["slug"]}/{w["downloads"]["pdf"]}'
        meta = f'<time datetime="{esc(w["date_iso"])}">{esc(w["date_label"])}</time>'
        if repo:
            # The repository itself, not a subfolder: the files the report names
            # are linked individually where it names them.
            meta += f'<span class="noprint"> · <a href="{esc(repo)}">Repository</a></span>'
        # No `download` attribute: a same-origin PDF opens in the browser's viewer,
        # which still saves it, and a policy that blocks downloads cannot eat the link.
        meta += f'<span class="noprint"> · <a href="{esc(pdf_href)}">PDF</a></span>'

        canonical = f"{self.base}/{w['slug']}"
        page_body = fill(read_text(HERE / "templates" / "work.html"),
                         masthead=self.masthead(current=self.work_nav, mode="true"),
                         title=title_html, meta=meta, canonical=esc(canonical),
                         content=body.strip("\n"), footer=self.footer())
        self.pages[f"{w['slug']}/index.html"] = self.page(
            title=f"{title_text} · {self.cfg['name']}", description=w["description"],
            canonical=canonical, og_title=title_text, og_type="article", body=page_body)
        self.urls.append(canonical)
        return {"page": w["slug"], "code_linkable": sorted(set(ctx.code_linkable)),
                "code_linked": sorted(set(ctx.code_linked))}

    # -------------------------------------------------------------- method
    def doc_page(self, spec: dict, content: str) -> None:
        slug = spec["slug"]
        canonical = f"{self.base}/{slug}"
        # The page's own address closes <main>, print only: a printout has no
        # masthead or footer, and this keeps its provenance on the paper.
        body = fill(read_text(HERE / "templates" / "doc.html"),
                    masthead=self.masthead(current=f"/{slug}", mode="page"),
                    content=content.strip("\n"), canonical=esc(canonical), footer=self.footer())
        title = f"{spec['title']} · {self.cfg['name']}"
        self.pages[f"{slug}/index.html"] = self.page(
            title=title, description=spec["description"],
            canonical=canonical, og_title=title, og_type="article", body=body)
        self.urls.append(canonical)

    def intro(self, spec: dict) -> tuple[str, Ctx]:
        src = HERE / spec["intro"]
        ctx = Ctx(self.cfg, src, spec["intro"], self.asset_root, self.tracked)
        out = render_md(read_text(src), ctx).strip()
        m = re.match(r"<h1\b[^>]*>(.*?)</h1>", out, re.S)
        if not m or strip_tags(m[1]) != spec["title"]:
            fail(f"{spec['intro']} must open with '# {spec['title']}'")
        if len(re.findall(r"<h1\b", out)) != 1:
            fail(f"{spec['intro']} has more than one '# ' heading")
        self.check_repo_links(out, spec["intro"])
        return out, ctx

    def method(self) -> dict:
        spec = self.cfg["method"]
        intro, ictx = self.intro(spec)
        parts = [intro]
        slugs = {w["slug"] for w in self.published}
        linked, linkable = list(ictx.code_linked), list(ictx.code_linkable)
        for p in spec["panels"]:
            if p["work"] not in slugs:
                fail(f"method panel {p['heading']!r} names work {p['work']!r}, which is not a published work")
            src = HERE / p["from"]
            ctx = Ctx(self.cfg, src, p["from"], self.asset_root, self.tracked)
            out = render_md(method_panel_md(p), ctx).strip()
            if re.search(r"<h[12]\b", out):
                fail(f"{p['from']}: the extracted block still has an h1/h2 after demotion")
            # The run command is for copying: it must survive rendering exactly
            # (no smart dashes or quotes inside code).
            cmd = re.search(r"`([^`]+)`", extract_line(src, p["run_line"]))
            codes = [strip_tags(c) for c in re.findall(r"<code\b[^>]*>(.*?)</code>", out, re.S)]
            if cmd and cmd[1] not in codes:
                fail(f"{p['from']}: the run command did not render verbatim: {cmd[1]!r} not in {codes[:2]}")
            # Namespace the block's heading ids by its work: /method#adv-limits.
            pid = p["work"]
            out = re.sub(r'<(h[1-6]) id="([^"]+)"', lambda m: f'<{m[1]} id="{pid}-{m[2]}"', out)
            parts.append(f'<h2 id="{pid}">{esc(p["heading"])}</h2>\n{out}')
            linked += ctx.code_linked
            linkable += ctx.code_linkable
        self.doc_page(spec, "\n".join(parts))
        return {"page": spec["slug"], "code_linkable": sorted(set(linkable)),
                "code_linked": sorted(set(linked))}

    # -------------------------------------------------------------- data
    def data(self) -> tuple[Path, str]:
        spec = self.cfg["data"]
        intro, _ = self.intro(spec)
        # Bold is the only visual mark of the panel files; screen readers do not
        # announce it. The intro paragraph that says so gets an id, and the two
        # panel links point at it with aria-describedby. Page text is unchanged.
        note_id = None
        if spec.get("panel_note"):
            paras = [m for m in re.finditer(r"<p>(.*?)</p>", intro, re.S)
                     if spec["panel_note"] in strip_tags(m[1])]
            if len(paras) != 1:
                fail(f"{spec['intro']}: expected one paragraph containing {spec['panel_note']!r} "
                     f"(data.panel_note), found {len(paras)}")
            note_id = spec.get("panel_note_id", "panel-files")
            intro = intro[:paras[0].start()] + f'<p id="{note_id}">' + intro[paras[0].start() + len("<p>"):]
        variants = [s["variant"] for s in spec["sections"]]
        labels = spec["source_labels"]
        src = (HERE / spec["manifest"]).resolve()
        rows = read_manifest(src, variants, labels)

        stale = stale_files(rows)
        counts = Counter(r["variant_by_content"] for r in rows)
        print(f"  data: {len(rows)} files in {spec['manifest']} "
              f"({', '.join(f'{v} {counts[v]}' for v in variants)}); stale set: {sorted(stale)}")
        expected = set(spec["expected_stale"])
        if stale != expected:
            fail(f"stale files {sorted(stale)} differ from site.json data.expected_stale "
                 f"{sorted(expected)}. A manifest change made a file stale (or fresh): look at it, "
                 f"then update expected_stale deliberately.")

        panel = spec["panel_files"]
        checksums = read_text(HERE / spec["panel_checksums_in"])
        for name in panel:
            hit = [r for r in rows if r["filename"] == name]
            if len(hit) != 1:
                fail(f"panel file {name} is listed {len(hit)} times in {spec['manifest']}")
            if hit[0]["sha256"] not in checksums:
                fail(f"panel file {name}: the manifest's sha256 {hit[0]['sha256']} is not the one "
                     f"in {spec['panel_checksums_in']}")

        mark = spec["stale_mark"]
        if not spec["stale_note"].startswith(mark):
            fail("data.stale_note must start with data.stale_mark")
        sections = []
        for s in spec["sections"]:
            hid = slugify(s["heading"])
            mine = sorted((r for r in rows if r["variant_by_content"] == s["variant"]),
                          key=lambda r: r["filename"])
            mine.sort(key=lambda r: r["vintage"], reverse=True)
            trs = []
            for r in mine:
                name, sha = r["filename"], r["sha256"]
                described = f' aria-describedby="{note_id}"' if name in panel and note_id else ""
                link = f'<a href="{esc(r["url"])}"{described}>{esc(name)}</a>'
                if name in panel:
                    link = f"<strong>{link}</strong>"
                through = esc(r["data_through"]) + (f" {esc(mark)}" if name in stale else "")
                tr = ' class="panel-file"' if name in panel else ""
                # The file name is the row's header: a screen reader moving down
                # the sha256 or "Data through" column hears which file it is in.
                trs.append(f'<tr{tr}>\n<th scope="row">{link}</th>\n<td>{esc(r["vintage"])}</td>\n'
                           f"<td>{through}</td>\n<td class=\"num\">{int(r['n_rows']):,}</td>\n"
                           f"<td>{esc(labels[r['source']])}</td>\n"
                           f'<td><code title="{esc(sha)}">{esc(sha[:12])}</code></td>\n</tr>')
            sections.append(
                f'<h2 id="{hid}">{esc(s["heading"])}</h2>\n'
                f'<div class="table-wrap bleed" role="region" aria-labelledby="{hid}" tabindex="0">\n'
                '<table class="files">\n<thead>\n<tr>\n<th>File</th>\n<th>File date</th>\n'
                '<th>Data through</th>\n<th class="num">Rows</th>\n<th>Source</th>\n<th>sha256</th>\n'
                '</tr>\n</thead>\n<tbody>\n' + "\n".join(trs) + "\n</tbody>\n</table>\n</div>")
        note = f'<p class="note">{esc(spec["stale_note"])}</p>'
        self.doc_page(spec, "\n".join([intro] + sections + [note]))
        return src, f"{spec['slug']}/{spec['manifest_name']}"

    # -------------------------------------------------------------- 404
    def not_found(self):
        body = fill(read_text(HERE / "templates" / "404.html"),
                    masthead=self.masthead(), footer=self.footer())
        self.pages["404.html"] = self.page(
            title=f"Page not found · {self.cfg['name']}", description="There is no page at this address.",
            canonical=None, og_title="Page not found", og_type="website", body=body,
            robots='<meta name="robots" content="noindex">\n')


def inline_md_with_links(text: str, ctx: Ctx) -> str:
    out = render_md(text, ctx).strip()
    m = re.fullmatch(r"<p>(.*)</p>", out, flags=re.S)
    if not m:
        fail(f"appendix note must be one paragraph: {text!r}")
    return m[1]


def forbidden_strings(cfg: dict) -> list[str]:
    """The published works' forbidden strings from their checks files (the gate's
    list), so a retracted figure fails the build before it reaches verify.py."""
    out = []
    for w in cfg["works"]:
        p = HERE / w.get("checks", "")
        if status_of(w) != PUBLISHED or not w.get("checks") or not p.is_file():
            continue
        try:
            out += json.loads(read_text(p)).get("forbidden", [])
        except (ValueError, AttributeError) as e:
            print(f"  warning: cannot read forbidden strings from {p.name}: {e}")
    return list(dict.fromkeys(out))


def vercel_json() -> str:
    cfg = {
        "cleanUrls": True,
        "trailingSlash": False,
        # "/:path*" does not match the bare "/" on Vercel, which left
        # https://www.filingstrail.com/ serving a 200. A regex source does.
        "redirects": [{
            "source": "/(.*)",
            "has": [{"type": "host", "value": "www.filingstrail.com"}],
            "destination": "https://filingstrail.com/$1",
            "permanent": True,
        }],
        "headers": [{
            "source": "/(.*)",
            "headers": [
                {"key": "X-Content-Type-Options", "value": "nosniff"},
                {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
            ],
        }],
    }
    return json.dumps(cfg, indent=2) + "\n"


def clean_dist(dist: Path) -> None:
    # Never empty a directory that is not a previous build (a mistyped --dist).
    if dist == HERE or dist in HERE.parents:
        fail(f"refusing to use {dist} as the output directory")
    if dist != DIST and dist.is_dir() and any(dist.iterdir()) and not (dist / "index.html").is_file():
        fail(f"refusing to empty {dist}: not empty and not a previous build")
    dist.mkdir(parents=True, exist_ok=True)
    for child in sorted(dist.iterdir()):
        if child.name in KEEP_IN_DIST:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except PermissionError as e:
            fail(f"cannot remove {child} ({e}); is it open in another program?")


def norm_text(s: str) -> str:
    return " ".join(html.unescape(s).split()).casefold()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assets", type=Path, default=HERE,
                    help="directory that site.json asset paths are relative to (default: site/)")
    ap.add_argument("--config", type=Path, default=HERE / "site.json",
                    help="alternate config, for testing (its paths still resolve from site/)")
    ap.add_argument("--dist", type=Path, default=DIST,
                    help="output directory, for testing (default: site/dist)")
    args = ap.parse_args(argv)
    dist = args.dist.resolve()

    cfg = json.loads(read_text(args.config))
    site = Site(cfg, args.assets.resolve())
    slugs = [w["slug"] for w in site.published]
    reserved = [cfg["method"]["slug"], cfg["data"]["slug"]]
    every = slugs + reserved
    if len(set(every)) != len(every) or not all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", s) for s in every):
        fail(f"work, method and data slugs must be unique lowercase url segments: {every}")

    site.root()
    reports = [site.work(w) for w in site.published]
    reports.append(site.method())
    manifest_src, manifest_rel = site.data()
    site.not_found()

    checks = {name: check_page(name, page, cfg["analytics_src"]) for name, page in site.pages.items()}
    named = [n for n, p in site.pages.items() if cfg["author"] in p]
    if named != ["index.html"] or site.pages["index.html"].count(cfg["author"]) != 1:
        fail(f"the author's name must appear exactly once, on the root page; found on {named}")

    downloads = {f'{w["slug"]}/{w["downloads"]["csv"]["name"]}': site.asset_root / w["downloads"]["csv"]["from"]
                 for w in site.published}
    downloads[manifest_rel] = manifest_src
    missing = [p for p in downloads.values() if not p.is_file()]
    if missing:
        fail(f"missing download {missing[0]} (run charts.py)")
    pdfs = {f'{w["slug"]}/{w["downloads"]["pdf"]}' for w in site.published}    # pdf.py writes these
    check_links(site.pages, checks, set(downloads) | pdfs)

    banned = forbidden_strings(cfg)
    for name, text in list(site.pages.items()) + [(manifest_rel, manifest_src.read_bytes().decode("utf-8", "replace"))]:
        hits = [s for s in banned if norm_text(s) in norm_text(text)]
        if hits:
            fail(f"{name} contains forbidden strings {hits}")

    clean_dist(dist)
    for name, page in site.pages.items():
        write_text(dist / name, page)
    for rel, src in downloads.items():
        (dist / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dist / rel)
    base = site.base
    write_text(dist / "robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    write_text(dist / "sitemap.xml",
               '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "".join(f"  <url><loc>{esc(u)}</loc></url>\n" for u in site.urls)
               + "</urlset>\n")
    write_text(dist / "vercel.json", vercel_json())

    for f in sorted(p for p in dist.rglob("*") if p.is_file() and ".vercel" not in p.parts):
        print(f"  {f.relative_to(dist).as_posix():<44} {f.stat().st_size:>9,} bytes")
    print(f"  inline CSS {len(site.css.encode()):,} bytes")
    for r in reports:
        if cfg["repo_url"]:
            print(f"  {r['page']}: code spans linked to the repository: {', '.join(r['code_linked']) or 'none'}")
        else:
            print(f"  {r['page']}: repo_url not set; Repository link omitted, code spans left "
                  f"unlinked ({', '.join(r['code_linkable']) or 'none'} would link)")
    print(f"built {len(site.pages)} pages into {dist}; next: python pdf.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        sys.exit(1)
