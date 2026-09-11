#!/usr/bin/env python3
"""verify.py - the deploy gate for filingstrail.com.

Independent of the generator: it parses what is actually in SITE/dist (or, with
--live, what the production host actually serves) and fails loudly. Exit code 1 on
ANY failure. A run that skipped a required check (--no-browser) also exits 1: only a
complete run can say "deployable". (M3 without build/adv/report_stats.log is an optional
SKIP: printed, but it does not block.)

    python site/verify.py                         pre-deploy gate over site/dist
    python site/verify.py --allow-missing-repo    same, repo_url null downgraded to WARN
    python site/verify.py --live https://filingstrail.com    post-deploy checks

Checks (dist mode)
  A  figures: the user's numbers gate, row by row (checks/<work>.json), on the HTML
     page, on the PDF text, and the root page's figure budget
  B  forbidden strings in every file of dist (HTML/SVG/CSS/JSON/TXT/XML/CSV/PDF)
  C  author name exactly once (root page, last line); email once visible + one mailto.
     The owner handle in the pinned repo_url is exempt only inside href="<repo_url>[/?#...]"
  D  page weight (HTML + same-origin subresources + analytics allowance) < limit
  E  no-JS / no third party: static rules + JS-off headless render == static text
  F  mobile: tables/charts in scroll containers + measured overflow at 320/375 px
  G  links and ids: root-absolute links resolve (cleanUrls), no relative links,
     unique ids, aria targets, fragment targets, deploy files (vercel.json, sitemap)
  H  repo link: repo_url set in site.json and linked on /adv before the first <h2>
  I  PDF link: /adv links the PDF before the first <h2>; the PDF is valid
  J  CSV: the firm-level CSV parses and reconciles to the gate counts
  K  chart colours: CSS variables used by inline charts are defined (light + dark)
  K2 chart-label halo: a .chart text {paint-order:stroke; stroke:var(--bg)...} rule reaches every label,
     and no @media rule (dark, screen) that would win the cascade switches it off; a print rule may,
     only if checks/pages.json allows it (halo.print_without_halo) and every label that sits on a
     filled shape in the PDF clears the contrast floor there
  H2 every link into the repository names the default branch and a path that exists in the repo
  N1 masthead on every page: wordmark -> /, nav[aria-label=Site] Work/Method/Data, aria-current,
     the root's <h1> is the wordmark, #work exists; no old header.site
  N2 root: existing copy verbatim, work list (published item linked first, in-progress items
     unlinked with an "In progress" label), footer unchanged
  N3 footer on every other page: one line, "Code on GitHub" -> repo_url, no name
  N4 masthead and footer hidden in print (@media print rule + absent from the PDF text)
  N5 sitemap.xml lists exactly the pages in checks/pages.json
  M1 /method: title/description, content/method-intro.md verbatim (and == the pinned copy),
     outline h1/h2/h3, METHOD.md "Run:" line + "### The file-selection trap" to the end word for
     word less the dropped sentence, real tables == METHOD.md, numeric columns right-aligned
  M2 /method numeric provenance: every number is in METHOD.md (less the dropped sentence) or REPORT.md
  M3 /method churn pairs == build/adv/report_stats.log section 8 (optional SKIP without the log)
  D1 /data tables == archive/MANIFEST.csv row for row; intro verbatim
  D2 the two panel files: bold + class, rows/sha256/data-through/url equal on /data, /adv Sources,
     METHOD.md Sources and MANIFEST
  D3 daggers == the stale rule on MANIFEST == checks/pages.json expected_stale; note under the tables
  D4 dist/data/manifest.csv is byte-identical to archive/MANIFEST.csv

  Live mode fetches /, the work pages, /method, /data, /data/manifest.csv, sitemap.xml and the 404
  page, and runs A, B, C, N1-N5, M1-M3 and D1-D4 on the served bytes.

Dependencies: Python stdlib + PyMuPDF (fitz) + pypdf. Headless Edge (Chrome fallback)
for E/F dynamic checks, talking only to a local 127.0.0.1 server.

Browser notes (measured on Edge/Chrome 152, headless=new, Windows):
  * --blink-settings=scriptEnabled=false makes --dump-dom and --screenshot return
    nothing, so "JS off" is done by serving each page with the header
    'Content-Security-Policy: script-src none' (no inline, external or on* script runs).
  * --window-size below ~500px is clamped (a 320px window reports innerWidth 492), so
    320/375/390px viewports are emulated with an iframe of exactly that width inside a
    harness page; the probe reads scrollWidth/innerWidth from the framed page.
  * Launched from Git Bash, msedge.exe detaches from its launcher and --dump-dom output
    is lost; the gate then falls back to Chrome and says so. From PowerShell, Edge works.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import gzip
import hashlib
import html
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIST = HERE / "dist"
DEFAULT_CHECKS = HERE / "checks"
DEFAULT_OUT = DEFAULT_CHECKS / "out"

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

try:  # never crash on a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# =================================================================== reporting

class Report:
    """Collects PASS/FAIL/WARN/INFO/SKIP lines; prints as it goes."""

    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.lines: list[str] = []
        self.optional: set[int] = set()   # indexes of SKIP rows that do not block (see skip_optional)

    _ASCII = str.maketrans({"·": "|", "‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", "…": "...", " ": " ", "→": "->",
                            "−": "-", " ": " ", " ": " "})

    def out(self, s: str = "") -> None:
        s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", s)  # damaged files must not break the report
        s = s.translate(self._ASCII)  # output gets pasted from consoles with legacy code pages
        print(s, flush=True)
        self.lines.append(s)

    def section(self, title: str) -> None:
        self.out("")
        self.out(title)
        self.out("-" * len(title))

    def add(self, status: str, cid: str, msg: str, details=()) -> None:
        msg = re.sub(r"\s*[\r\n]+\s*", " ", str(msg))
        self.rows.append((status, cid, msg))
        self.out(f"[{status}] {cid:<4} {msg}")
        for d in details:
            self.out(f"             {re.sub(r'\s*[\r\n]+\s*', ' ', str(d))}")

    def ok(self, cid, msg, details=()):
        self.add("PASS", cid, msg, details)

    def fail(self, cid, msg, details=()):
        self.add("FAIL", cid, msg, details)

    def warn(self, cid, msg, details=()):
        self.add("WARN", cid, msg, details)

    def info(self, cid, msg, details=()):
        self.add("INFO", cid, msg, details)

    def skip(self, cid, msg, details=()):
        self.add("SKIP", cid, msg, details)

    def skip_optional(self, cid, msg, details=()):
        """A SKIP for a check that only runs when an optional input exists (M3 without
        build/adv/report_stats.log). Printed as SKIP; does not make the run INCOMPLETE."""
        self.add("SKIP", cid, msg + " [optional input missing; does not block]", details)
        self.optional.add(len(self.rows) - 1)

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.rows if s == status)

    def blocking_skips(self) -> int:
        return sum(1 for i, (s, _, _) in enumerate(self.rows) if s == "SKIP" and i not in self.optional)


def ascii_ctx(s: str, n: int = 160) -> str:
    s = s.replace("\n", " ")
    return (s[:n] + "...") if len(s) > n else s


# ============================================================ text normalization

_ZW = {ord(c): None for c in "\u00ad\u200b\u200c\u200d\u2060\ufeff"}
_PUNCT = {}
_PUNCT.update({ord(c): "'" for c in "\u2018\u2019\u201a\u201b\u2032"})
_PUNCT.update({ord(c): '"' for c in "\u201c\u201d\u201e\u201f\u2033"})
_PUNCT.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})
_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    """NFKC (NBSP/thin spaces -> space, ligatures split), zero-width and soft hyphens
    removed, curly quotes -> straight, dashes/minus -> '-', whitespace collapsed."""
    s = unicodedata.normalize("NFKC", s).translate(_ZW).translate(_PUNCT)
    return _WS.sub(" ", s).strip()


def norm_ci(s: str) -> str:
    return norm(s).casefold()


def ctx_around(text: str, start: int, end: int, pad: int = 50) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("..." if a else "") + text[a:b] + ("..." if b < len(text) else "")


# ======================================================================== DOM

VOID = frozenset("area base br col embed hr img input link meta param source track wbr keygen".split())
P_CLOSERS = frozenset(("address article aside blockquote details dialog div dl fieldset figcaption figure "
                       "footer form h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol p pre section "
                       "table ul").split())
INLINE = frozenset(("a abbr b bdi bdo cite code data del dfn em i ins kbd label mark q s samp small span "
                    "strong sub sup time u var wbr tspan textpath").split())
NONVISIBLE = frozenset("head script style template noscript title meta link".split())
SVG_NONVISIBLE = frozenset(("metadata defs clippath mask symbol marker pattern lineargradient radialgradient "
                            "filter desc title style script").split())


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "data", "pos")

    def __init__(self, tag, attrs=None, parent=None, data=None, pos=(0, 0)):
        self.tag = tag
        self.attrs: dict[str, str | None] = {}
        for k, v in attrs or []:
            self.attrs.setdefault(k, v)
        self.children: list[Node] = []
        self.parent = parent
        self.data = data
        self.pos = pos

    def get(self, k, d=None):
        return self.attrs.get(k, d)

    def iter(self):
        """Pre-order (document order) over element nodes."""
        stack = [self]
        while stack:
            n = stack.pop()
            if n.tag is not None:
                yield n
                stack.extend(reversed(n.children))

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def text(self) -> str:
        return "".join(c.data if c.tag is None else c.text() for c in self.children)

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def desc(self) -> str:
        s = self.tag or "#text"
        if self.get("id"):
            s += "#" + self.get("id")
        if self.get("class"):
            s += "." + ".".join(self.get("class").split())
        return s


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]
        self.comments: list[str] = []
        self.stray_end: list[str] = []

    def _close(self, names, stop):
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t in names:
                del self.stack[i:]
                return
            if t in stop:
                return

    def _implied(self, tag):
        if tag in P_CLOSERS:
            self._close({"p"}, {"div", "section", "article", "td", "th", "li", "body", "html", "button",
                                "table", "svg", "figure", "blockquote", "main", "header", "footer", "nav"})
        if tag == "li":
            self._close({"li"}, {"ul", "ol", "menu"})
        elif tag in ("dt", "dd"):
            self._close({"dt", "dd"}, {"dl"})
        elif tag in ("td", "th"):
            self._close({"td", "th"}, {"tr", "table"})
        elif tag == "tr":
            self._close({"tr"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in ("thead", "tbody", "tfoot"):
            self._close({"thead", "tbody", "tfoot"}, {"table"})
        elif tag == "option":
            self._close({"option"}, {"select", "datalist"})

    def handle_starttag(self, tag, attrs):
        self._implied(tag)
        n = Node(tag, attrs, self.stack[-1], pos=self.getpos())
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self._implied(tag)
        n = Node(tag, attrs, self.stack[-1], pos=self.getpos())
        self.stack[-1].children.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        self.stray_end.append(tag)

    def handle_data(self, data):
        self.stack[-1].children.append(Node(None, parent=self.stack[-1], data=data))

    def handle_comment(self, data):
        self.comments.append(data)


def parse_html(src: str) -> TreeBuilder:
    tb = TreeBuilder()
    tb.feed(src)
    tb.close()
    return tb


def visible_blocks(node: Node) -> list[str]:
    """Visible text as a list of normalized blocks. Skips head/script/style/template/
    noscript/title and SVG metadata/defs/title/desc; keeps SVG <text>/<tspan>."""
    blocks: list[str] = []
    cur: list[str] = []

    def flush():
        if cur:
            t = norm("".join(cur))
            cur.clear()
            if t:
                blocks.append(t)

    def walk(n: Node, in_svg: bool):
        for c in n.children:
            if c.tag is None:
                cur.append(c.data)
                continue
            t = c.tag
            if t in NONVISIBLE or (in_svg and t in SVG_NONVISIBLE):
                continue
            if t == "br":
                cur.append(" ")
                continue
            s = in_svg or t == "svg"
            if t in INLINE:
                walk(c, s)
            else:
                flush()
                walk(c, s)
                flush()

    walk(node, False)
    flush()
    return blocks


_SENT = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"')\]]))\s+(?=[\"'(\[]?[A-Z0-9$])")


def sentences(blocks):
    for b in blocks:
        for s in _SENT.split(b):
            if s.strip():
                yield s.strip()


def clauses(blocks):
    for s in sentences(blocks):
        for c in re.split(r";\s+", s):
            if c.strip():
                yield c.strip()


def rel_to_url(rel: str) -> str:
    if rel == "index.html":
        return "/"
    if rel.endswith("/index.html"):
        return "/" + rel[: -len("/index.html")]
    if rel.endswith(".html"):
        return "/" + rel[: -len(".html")]
    return "/" + rel


def slug(rel: str) -> str:
    s = rel[: -len("/index.html")] if rel.endswith("/index.html") else rel
    s = s[: -len(".html")] if s.endswith(".html") else s
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "root"


def resolve_path(path: str, files) -> str | None:
    """Root-absolute URL path -> dist-relative file, honouring Vercel cleanUrls."""
    p = urllib.parse.unquote(path.split("#", 1)[0].split("?", 1)[0])
    if not p.startswith("/"):
        return None
    if p == "/":
        cands = ["index.html"]
    elif p.endswith("/"):
        cands = [p[1:] + "index.html"]
    else:
        cands = [p[1:], p[1:] + ".html", p[1:] + "/index.html"]
    for c in cands:
        if c in files:
            return c
    return None


def url_host(u: str) -> str | None:
    u = u.strip()
    if u.startswith("//"):
        return (urllib.parse.urlsplit("https:" + u).hostname or "").lower()
    sp = urllib.parse.urlsplit(u)
    if sp.scheme.lower() in ("http", "https", "ftp", "ws", "wss"):
        return (sp.hostname or "").lower()
    return None


def srcset_urls(v: str) -> list[str]:
    return [part.strip().split()[0] for part in v.split(",") if part.strip()]


_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)


# ======================================================================== CSS

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def css_rules(css: str):
    """(selector_or_at_prelude, body, context_preludes) for every rule; @media/@supports
    blocks are flattened with their prelude kept in the context."""
    css = _CSS_COMMENT.sub("", css)
    out = []

    def parse(s: str, ctx: list[str]):
        i, n = 0, len(s)
        while i < n:
            j = s.find("{", i)
            if j < 0:
                break
            head = re.sub(r"^(\s*@[^;{]*;)+", "", s[i:j]).strip()
            depth, k = 1, j + 1
            while k < n and depth:
                if s[k] == "{":
                    depth += 1
                elif s[k] == "}":
                    depth -= 1
                k += 1
            body = s[j + 1: k - 1]
            if head.startswith("@") and re.match(r"@(media|supports|layer|container|document)\b", head, re.I):
                parse(body, ctx + [head])
            else:
                out.append((head, body, ctx))
            i = k

    parse(css, [])
    return out


def css_decls(body: str) -> dict[str, str]:
    d = {}
    for part in body.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d


def decl_gives_xscroll(d: dict[str, str]) -> bool:
    v = d.get("overflow-x") or d.get("overflow")
    if not v:
        return False
    return v.replace("!important", "").split()[0].lower() in ("auto", "scroll")


def selector_keys(selector: str) -> set[str]:
    """Classes ('.x') and tag names of the last compound of each selector in a list."""
    keys = set()
    for sel in selector.split(","):
        sel = sel.strip()
        if not sel:
            continue
        last = re.split(r"\s*[>+~]\s*|\s+", sel)[-1]
        last = re.sub(r"::?[\w-]+(\([^)]*\))?", "", last)
        for c in re.findall(r"\.([\w-]+)", last):
            keys.add("." + c)
        m = re.match(r"^([a-zA-Z][\w-]*)", last)
        if m:
            keys.add(m.group(1).lower())
    return keys


def is_dark_ctx(selector: str, ctx: list[str]) -> bool:
    c = " ".join(ctx).lower().replace(" ", "")
    return ("prefers-color-scheme:dark" in c) or bool(
        re.search(r"data-theme\s*=\s*['\"]?dark|\.dark\b|\[theme=['\"]?dark", selector, re.I))


# ======================================================================== PDF

def _fitz():
    try:
        import pymupdf as fitz  # noqa
    except ImportError:
        import fitz  # noqa
    return fitz


def pdf_extract(data: bytes) -> dict:
    res = {"ok": False, "pages": 0, "pymupdf": "", "pypdf": "", "meta": "", "errors": [],
           "encrypted": False, "pypdf_pages": 0}
    try:
        fitz = _fitz()
        doc = fitz.open(stream=data, filetype="pdf")
        res["pages"] = doc.page_count
        res["encrypted"] = bool(doc.is_encrypted)
        res["pymupdf"] = "\n".join(p.get_text("text") for p in doc)
        res["meta"] = " ".join(f"{k}={v}" for k, v in (doc.metadata or {}).items() if v)
        doc.close()
        res["ok"] = True
    except Exception as e:  # noqa
        res["errors"].append(f"PyMuPDF: {type(e).__name__}: {e}")
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        res["pypdf_pages"] = len(r.pages)
        res["pypdf"] = "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception as e:  # noqa
        res["errors"].append(f"pypdf: {type(e).__name__}: {e}")
    return res


def text_variants(t: str) -> list[str]:
    """Normalized variants of extracted PDF text: as-is; hyphen at a line break joined
    ('regis-\\ntration' -> 'registration'); hyphen kept ('re-\\nregistration' ->
    're-registration'). A figure check passes if any variant matches; a forbidden
    string hits if any variant contains it."""
    a = norm(t)
    b = norm(re.sub(r"(\w)[-\u2010\u00ad]\s*\n\s*(\w)", r"\1\2", t))
    c = norm(re.sub(r"(\w)([-\u2010])\s*\n\s*(\w)", r"\1-\3", t))
    return list(dict.fromkeys([a, b, c]))


# ===================================================================== bundle

TEXT_EXT = (".html", ".htm", ".svg", ".css", ".json", ".txt", ".xml", ".csv", ".js", ".mjs",
            ".webmanifest", ".md", ".map", ".ics", ".vcf")


class Page:
    def __init__(self, rel: str, data: bytes):
        self.rel = rel
        self.raw = data
        self.src = data.decode("utf-8", "replace")
        tb = parse_html(self.src)
        self.root = tb.root
        self.comments = tb.comments
        self.stray_end = tb.stray_end
        self.blocks = visible_blocks(self.root)
        self.text = " ".join(self.blocks)
        self.url = rel_to_url(rel)

    def nodes(self, *tags):
        return [n for n in self.root.iter() if n.tag in tags]

    def before_first(self, tag: str):
        """Element nodes in document order before the first <tag>."""
        out = []
        for n in self.root.iter():
            if n.tag == tag:
                break
            out.append(n)
        return out


class Bundle:
    def __init__(self, files: dict[str, bytes], origin: str):
        self.files = files
        self.origin = origin
        self._pages: dict[str, Page] = {}
        self._pdfs: dict[str, dict] = {}

    @classmethod
    def from_dist(cls, dist: Path) -> "Bundle":
        files = {}
        for p in sorted(dist.rglob("*")):
            if p.is_file():
                rel = p.relative_to(dist).as_posix()
                if rel.split("/")[0] == ".vercel":
                    continue
                files[rel] = p.read_bytes()
        return cls(files, str(dist))

    def html_rels(self):
        return [r for r in self.files if r.lower().endswith((".html", ".htm"))]

    def page(self, rel: str) -> Page:
        if rel not in self._pages:
            self._pages[rel] = Page(rel, self.files[rel])
        return self._pages[rel]

    def pdf(self, rel: str) -> dict:
        if rel not in self._pdfs:
            self._pdfs[rel] = pdf_extract(self.files[rel])
        return self._pdfs[rel]

    def searchable(self, rel: str) -> dict[str, list[str]]:
        """Normalized, searchable text per extraction method for one file."""
        low = rel.lower()
        data = self.files[rel]
        if low.endswith(".pdf"):
            px = self.pdf(rel)
            return {"pymupdf": text_variants(px["pymupdf"]), "pypdf": text_variants(px["pypdf"]),
                    "pdf-metadata": [norm(px["meta"])]}
        s = data.decode("utf-8", "replace")
        if low.endswith((".html", ".htm")):
            pg = self.page(rel)
            unesc = html.unescape(s)
            tagless = re.sub(r"<[^>]*>", "", re.sub(r"<!--.*?-->", "", s, flags=re.S))
            return {"raw": [norm(unesc), norm(urllib.parse.unquote(unesc))], "visible": [pg.text],
                    "tagless": [norm(html.unescape(tagless))]}
        if low.endswith((".svg", ".xml")):
            return {"raw": [norm(s), norm(html.unescape(s))]}
        return {"raw": [norm(s)]}


# ============================================================ gate evaluation

def load_works(checks_dir: Path) -> list[dict]:
    works = []
    for p in sorted(checks_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa
            raise SystemExit(f"cannot parse {p}: {e}")
        if isinstance(d, dict) and d.get("work"):
            d["_file"] = p.name
            works.append(d)
    return works


def table_rows(t: Node):
    out = []

    def walk(n):
        for c in n.children:
            if c.tag is None or c.tag == "table":
                continue
            if c.tag == "tr":
                cells = [x for x in c.children if x.tag in ("td", "th")]
                out.append((c, [cell_text(x) for x in cells], [x.tag for x in cells]))
            else:
                walk(c)

    walk(t)
    return out


def cell_text(x: Node) -> str:
    return re.sub(r"\*\*|__", "", norm(" ".join(visible_blocks(x)))).strip()


def check_table_html(root: Node, spec: dict):
    fails, oks = [], []
    tables = [t for t in root.iter() if t.tag == "table"]
    target = None
    firsts = []
    for t in tables:
        rows = table_rows(t)
        if not rows or not rows[0][1]:
            continue
        firsts.append(rows[0][1][0])
        if rows[0][2][0] == "th" and rows[0][1][0].startswith(spec["first_header_startswith"]):
            target = (t, rows)
            break
    if not target:
        fails.append(f'no <table> whose first header cell starts with "{spec["first_header_startswith"]}" '
                     f'({len(tables)} tables; first cells: {firsts})')
        return fails, oks
    t, rows = target
    head = [r for r in rows if all(k == "th" for k in r[2])]
    body = [r for r in rows if not all(k == "th" for k in r[2])]
    if spec.get("header") and head:
        got = head[0][1]
        exp = spec["header"]
        if len(got) != len(exp) or not all(fnmatch.fnmatchcase(g, e) for g, e in zip(got, exp)):
            fails.append(f"header row {got} != expected {exp}")
        else:
            oks.append(f"header {got}")
    exp_rows = spec["rows"]
    if len(body) != len(exp_rows):
        fails.append(f"{len(body)} body rows, expected {len(exp_rows)}: {[b[1] for b in body]}")
    for i, er in enumerate(exp_rows):
        if i >= len(body):
            break
        got = body[i][1]
        if len(got) != len(er):
            fails.append(f"row {i + 1}: {len(got)} cells {got}, expected {er}")
            continue
        for j, (g, e) in enumerate(zip(got, er)):
            if g != e:
                fails.append(f'row {i + 1} ("{er[0]}") col {j + 1}: expected "{e}", got "{g}"')
    if not any("row " in f for f in fails) and len(body) == len(exp_rows):
        oks.append("rows " + " | ".join(" ".join(r) for r in exp_rows))
    return fails, oks


def pdf_table_regexes(spec: dict) -> list[str]:
    return [r"(?<![\w.,])" + r"\s+".join(re.escape(c) for c in row) + r"(?![\w.,])" for row in spec["rows"]]


def eval_gate_row(row: dict, variants: list[str], blocks_list: list[list[str]], root: Node | None):
    """variants: normalized full texts (one for HTML, several for PDF); blocks_list: the
    same texts as block lists (for sentence/clause units); root: DOM for table parsing."""
    fails, oks = [], []
    for s in row.get("must_contain", []):
        if any(s in v for v in variants):
            oks.append(f'"{s}"')
        else:
            fails.append(f'missing "{s}"')
    for rx in row.get("must_match", []):
        m = next((m for v in variants for m in [re.search(rx, v)] if m), None)
        if m:
            oks.append(f'"{m.group(0)}"')
        else:
            fails.append(f"no match for /{rx}/")
    for rx in row.get("must_not_match", []):
        for v in variants:
            hits = list(re.finditer(rx, v, re.I))
            if hits:
                h = hits[0]
                fails.append(f"forbidden /{rx}/ matched {len(hits)}x, e.g. \"{ctx_around(v, h.start(), h.end())}\"")
                break
    if "table" in row:
        if root is not None:
            f, o = check_table_html(root, row["table"])
            fails += f
            oks += o
        else:
            for rx, er in zip(pdf_table_regexes(row["table"]), row["table"]["rows"]):
                if any(re.search(rx, v) for v in variants):
                    oks.append("row " + " ".join(er))
                else:
                    fails.append("table row not found in text: " + " ".join(er))
    for u in row.get("units", []):
        fn = clauses if u.get("unit") == "clause" else sentences
        need = [a.casefold() for a in u.get("all", [])]
        need_re = u.get("all_re", [])
        hit = None
        for blocks in blocks_list:
            for s in fn(blocks):
                sc = s.casefold()
                if all(a in sc for a in need) and all(re.search(rx, s) for rx in need_re):
                    hit = s
                    break
            if hit:
                break
        if hit:
            oks.append(f'{u["name"]}: "{ascii_ctx(hit, 110)}"')
        else:
            near = [s for blocks in blocks_list[:1] for s in fn(blocks) if re.search(r"\bz\s*=", s)]
            fails.append(f'{u["name"]}: no single {u.get("unit", "sentence")} contains '
                         f'{u.get("all", []) + need_re}' + (f'; z-bearing units: {[ascii_ctx(x, 90) for x in near[:4]]}' if near else ""))
    if "allowed_z" in row or "allowed_p" in row:
        v = variants[0]
        zs = set(re.findall(r"\bz\s*=\s*(-?\d+(?:\.\d+)?)", v))
        ps = set(re.findall(r"\bp\s*[=<]\s*(\d*\.?\d+)", v))
        bz = sorted(zs - set(row.get("allowed_z", [])))
        bp = sorted(ps - set(row.get("allowed_p", [])))
        if bz or bp:
            fails.append(f"unexpected test statistics on the page: z={bz} p={bp} "
                         f"(allowed z={row.get('allowed_z')} p={row.get('allowed_p')})")
    return fails, oks


def meta_texts(pg: Page) -> list[str]:
    """Text that is not in the body but still shows up (tab title, search snippets, link
    previews, tooltips, screen readers): <title>, meta description/og/twitter content,
    alt/title/aria-label attributes."""
    out = []
    for n in pg.root.iter():
        if n.tag == "title" and not any(a.tag == "svg" for a in n.ancestors()):
            out.append(norm(n.text()))
        elif n.tag == "meta":
            key = (n.get("name") or n.get("property") or "").lower()
            if key in ("description", "keywords", "author") or key.startswith(("og:", "twitter:")):
                if key not in ("og:url", "og:image", "twitter:image", "og:type", "twitter:card"):
                    out.append(norm(n.get("content") or ""))
        for k in ("alt", "title", "aria-label"):
            if n.get(k) and n.tag != "title":
                out.append(norm(n.get(k)))
    return [t for t in out if t]


def svg_top(root: Node) -> list[Node]:
    return [n for n in root.iter() if n.tag == "svg" and not any(a.tag == "svg" for a in n.ancestors())]


def svg_lines(svg: Node) -> list[str]:
    return [t for t in (norm(n.text()) for n in svg.iter() if n.tag == "text") if t]


def identify_chart(svgs: list[Node], ch: dict, taken: set[int]):
    for s in svgs:
        if id(s) in taken:
            continue
        txt = " ".join(svg_lines(s))
        if any(k in txt for k in ch.get("identify_text_any", [])):
            return s
    for s in svgs:
        if id(s) in taken:
            continue
        ns = [s]
        fig = next((a for a in s.ancestors() if a.tag == "figure"), None)
        if fig is not None:
            ns.append(fig)
        if s.parent is not None:
            ns.append(s.parent)
        attrs = " ".join(v or "" for n in ns for k, v in n.attrs.items()
                         if k in ("id", "class", "aria-label", "data-chart", "data-name", "aria-labelledby"))
        if any(k in attrs for k in ch.get("identify_attr_any", [])):
            return s
    return None


# ================================================================= check A

def check_A(bundle: Bundle, works: list[dict], site: dict, R: Report, pdf_rels_override=None) -> None:
    R.section("A. FIGURES - the numbers gate (source of truth: adv-report/REPORT.md)")
    wl_forbid = site.get("window_length_must_not_match", [])
    for w in works:
        rel = w["page"]
        tag = w["work"]
        if rel not in bundle.files:
            R.fail("A", f"[{tag}] page {rel} is missing from {bundle.origin}")
            continue
        pg = bundle.page(rel)
        R.out(f"  {tag}: HTML page {rel}  ({len(pg.text):,} chars of visible text incl. inline-SVG <text>)")
        for i, row in enumerate(w["gate"], 1):
            f, o = eval_gate_row(row, [pg.text], [pg.blocks], pg.root)
            label = f'A{i}'
            head = f'{row["row"]}  [{row["value"]}]  html'
            (R.fail(label, head, f) if f else R.ok(label, head, o))
        # charts: inline <svg>, not <img>, expected text
        svgs = svg_top(pg.root)
        taken: set[int] = set()
        for j, ch in enumerate(w.get("charts", []), 1):
            label = f"A{len(w['gate']) + j}"
            s = identify_chart(svgs, ch, taken)
            if s is None:
                imgs = [n.get("src") for n in pg.nodes("img") if n.get("src")]
                R.fail(label, f'chart "{ch["name"]}": no inline <svg> found on {rel}',
                       [f"{len(svgs)} inline <svg> on page; <img> srcs: {imgs[:5]}"])
                continue
            taken.add(id(s))
            lines = svg_lines(s)
            joined = " ".join(lines)
            f = [f'svg text missing "{t}"' for t in ch.get("text_contains", []) if t not in joined]
            need = ch.get("a_line_contains_all", [])
            if need and not any(all(t in ln for t in need) for ln in lines):
                f.append(f"no single <text> line contains all of {need}; closest lines: "
                         f"{[ln for ln in lines if any(t in ln for t in need)][:3]}")
            if not lines:
                f.append("chart has no <text> elements (text rendered as paths?)")
            desc = f'chart "{ch["name"]}": inline <svg> with {len(lines)} <text> lines'
            (R.fail(label, desc, f) if f else R.ok(label, desc,
                                                  [f"contains {ch.get('text_contains', []) + need}"]))
        # no <img> charts, no .png anywhere
        chart_imgs = [n.get("src") for n in pg.nodes("img", "object", "embed", "image")
                      if any(k in (n.get("src") or n.get("data") or n.get("href") or "")
                             for k in ("new_registrants", "conversion_by_headcount", ".png", ".svg"))]
        pngs = [r for r in bundle.files if r.lower().endswith(".png")]
        png_refs = []
        for r in bundle.files:
            if r.lower().endswith(".pdf"):
                txt = " ".join(bundle.searchable(r)["pymupdf"])
                if ".png" in txt.lower():
                    png_refs.append(r)
            elif not r.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2")):
                if b".png" in bundle.files[r].lower():
                    png_refs.append(r)
        label = f"A{len(w['gate']) + len(w.get('charts', [])) + 1}"
        f = []
        if chart_imgs:
            f.append(f"charts referenced as <img>/<object>: {chart_imgs}")
        if pngs:
            f.append(f".png files in dist: {pngs}")
        if png_refs:
            f.append(f'".png" referenced in: {png_refs}')
        (R.fail(label, "charts are inline <svg>, no <img> charts, no .png in dist", f) if f
         else R.ok(label, "charts are inline <svg>, no <img> charts, no .png file or reference in dist"))

        # the PDF: same figures as text
        pdfs = pdf_rels_override if pdf_rels_override is not None else \
            [r for r in bundle.files if fnmatch.fnmatch(r, w.get("pdf_glob", "")) and r.lower().endswith(".pdf")]
        if not pdfs:
            R.fail("A-P", f"[{tag}] no PDF matching {w.get('pdf_glob')} - cannot run figure checks on the PDF")
        for prel in pdfs:
            px = bundle.pdf(prel)
            if not px["ok"] or not px["pymupdf"].strip():
                R.fail("A-P", f"{prel}: no text extracted by PyMuPDF", px["errors"])
                continue
            variants = text_variants(px["pymupdf"])
            R.out(f"  {tag}: PDF {prel}  ({px['pages']} pages, {len(variants[0]):,} chars via PyMuPDF)")
            for i, row in enumerate(w["gate"], 1):
                f, o = eval_gate_row(row, variants, [[v] for v in variants], None)
                head = f'{row["row"]}  pdf'
                (R.fail(f"P{i}", head, f) if f else R.ok(f"P{i}", head, o[:4]))
            for j, ch in enumerate(w.get("charts", []), 1):
                need = ch.get("text_contains", []) + ch.get("a_line_contains_all", [])
                miss = [t for t in need if not any(t in v for v in variants)]
                head = f'chart "{ch["name"]}" text  pdf'
                (R.fail(f"P{len(w['gate']) + j}", head, [f'missing "{m}"' for m in miss]) if miss
                 else R.ok(f"P{len(w['gate']) + j}", head, [f"contains {need}"]))

    # window-length wording on every page + PDF (not just the report)
    hits = []
    for rel in bundle.html_rels():
        for t in [bundle.page(rel).text] + meta_texts(bundle.page(rel)):
            for rx in wl_forbid:
                for m in re.finditer(rx, t, re.I):
                    hits.append(f'{rel}: /{rx}/ "{ctx_around(t, m.start(), m.end(), 40)}"')
    for rel in [r for r in bundle.files if r.lower().endswith(".pdf")]:
        for v in bundle.searchable(rel)["pymupdf"][:1]:
            for rx in wl_forbid:
                for m in re.finditer(rx, v, re.I):
                    hits.append(f'{rel}: /{rx}/ "{ctx_around(v, m.start(), m.end(), 40)}"')
    (R.fail("A-W", "a window length other than thirteen months appears", hits[:12]) if hits
     else R.ok("A-W", "no other window length (twelve / 12 months / 12-month / fourteen / eleven) on any page or PDF"))

    # the root page's figure budget
    root_rel = site.get("root_page", "index.html")
    if root_rel not in bundle.files:
        R.fail("A-R", f"root page {root_rel} missing")
        return
    rp = bundle.page(root_rel)
    must = [s for w in works for s in w.get("root_page", {}).get("must_contain", [])]
    phrases = [s for w in works for s in w.get("root_page", {}).get("allowed_phrases", [])]
    allowed = {s for w in works for s in w.get("root_page", {}).get("allowed_figures", [])}
    miss = [s for s in must if s not in rp.text]
    t = " | ".join([rp.text] + meta_texts(rp))                    # body + title/meta/link-preview text
    t = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", t)            # email addresses
    t = re.sub(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|org|net|io|gov)\S*", " ", t)  # urls / domains
    for p in phrases:
        t = t.replace(p, " ")
    bad = []
    for m in re.finditer(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?\s?%?", t):
        tok = m.group(0).strip().rstrip(",")
        digits = sum(ch.isdigit() for ch in tok)
        if digits >= 2 or tok.endswith("%"):
            if tok.lstrip("+-") not in allowed:
                bad.append(f'"{tok}" in "{ctx_around(t, m.start(), m.end(), 35)}"')
    f = [f'missing "{s}"' for s in miss] + [f"figure not allowed on the root page: {b}" for b in bad]
    links = {n.get("href") for n in rp.nodes("a")}
    for w in works:
        for target in w.get("root_page", {}).get("links_to", []):
            if target not in links:
                f.append(f'root page has no <a href="{target}">')
    (R.fail("A-R", f"root page ({root_rel}) figures (body, <title>, meta/og descriptions)", f) if f else
     R.ok("A-R", f"root page ({root_rel}) figures: has {must}; no other multi-digit number or % "
                 f"(allowed {sorted(allowed)}); links {[x for w in works for x in w.get('root_page', {}).get('links_to', [])]}"))


# ================================================================= check B

def check_B(bundle: Bundle, works: list[dict], R: Report, out_dir: Path | None, label="dist") -> None:
    R.section("B. FORBIDDEN STRINGS - every file, case-insensitive, whitespace-normalized")
    strings = list(dict.fromkeys(s for w in works for s in w.get("forbidden", [])))
    files = sorted(bundle.files)
    kinds = Counter(Path(r).suffix.lower() or "(none)" for r in files)
    unsearchable = [r for r in files if r.lower().endswith((".zip", ".gz", ".br", ".xlsx", ".docx", ".7z"))]
    matrix: dict[str, dict[str, tuple[int, list[str]]]] = {}
    contexts: list[str] = []
    for r in files:
        low = r.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf")):
            srch = {"raw": [norm(bundle.files[r].decode("utf-8", "ignore"))]}
        else:
            srch = bundle.searchable(r)
        matrix[r] = {}
        for s in strings:
            ns = norm_ci(s)
            best, how = 0, []
            for method, variants in srch.items():
                c = max((v.casefold().count(ns) for v in variants), default=0)
                if c:
                    how.append(f"{method}:{c}")
                    if c > best:
                        best = c
                        v0 = next(v for v in variants if v.casefold().count(ns))
                        k = v0.casefold().find(ns)
                        contexts.append(f'{r} [{method}] "{s}": "{ctx_around(v0, k, k + len(ns), 60)}"')
            matrix[r][s] = (best, how)
    lines = [f"Forbidden-string grep over {bundle.origin} ({label}) at {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"{len(files)} files: " + ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items())),
             "Method: NFKC + whitespace collapse + case-insensitive. HTML: entity-decoded raw source, URL-decoded raw, "
             "visible text (incl. inline SVG <text>), tag-stripped text. PDF: PyMuPDF text AND pypdf text (each with "
             "de-hyphenated variants) AND metadata. Other files: UTF-8 decoded raw.", ""]
    total_hits = 0
    for s in strings:
        tot = sum(matrix[r][s][0] for r in files)
        total_hits += tot
        where = [f"{r} ({matrix[r][s][0]}; {', '.join(matrix[r][s][1])})" for r in files if matrix[r][s][0]]
        line = f'"{s}"'.ljust(26) + f"{tot} hit{'s' if tot != 1 else ''}" + (f"  in {'; '.join(where)}" if where else "")
        lines.append(line)
        (R.fail if tot else R.ok)("B", line)
    lines += ["", "Per-file counts (columns in the order above):"]
    for r in files:
        lines.append("  " + " ".join(f"{matrix[r][s][0]:>2}" for s in strings) + "  " + r)
    if contexts:
        lines += ["", "Contexts:"] + ["  " + c for c in contexts]
    if unsearchable:
        lines += ["", f"UNSEARCHABLE archives in dist: {unsearchable}"]
        R.fail("B", f"opaque archive files in dist cannot be searched: {unsearchable}")
    R.out(f"             scanned {len(files)} files ({', '.join(f'{k} x{v}' for k, v in sorted(kinds.items()))}); "
          f"PDFs via PyMuPDF + pypdf")
    for r in files:
        if r.lower().endswith(".pdf"):
            px = bundle.pdf(r)
            R.out(f"             {r}: PyMuPDF {len(px['pymupdf']):,} chars, pypdf {len(px['pypdf']):,} chars, "
                  f"metadata {len(px['meta'])} chars")
            lines.insert(3, f"{r}: PyMuPDF {len(px['pymupdf']):,} chars, pypdf {len(px['pypdf']):,} chars")
            if not px["pymupdf"].strip() or not px["pypdf"].strip():
                R.fail("B", f"{r}: a PDF text extractor returned no text - forbidden-string search is blind there",
                       px["errors"])
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        name = "forbidden-grep.txt" if label == "dist" else f"forbidden-grep-{label}.txt"
        (out_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        R.out(f"             full grep written to {out_dir / name}")
    if total_hits == 0:
        R.ok("B", f"0 forbidden-string hits across {len(files)} files")


# ================================================================= check C

def check_C(bundle: Bundle, site: dict, R: Report) -> None:
    R.section("C. NAME AND EMAIL")
    name, email = site["author"], site["email"]
    root_rel = site.get("root_page", "index.html")
    name_rx = re.compile(r"\s+".join(map(re.escape, name.split())), re.I)
    word_rx = re.compile(r"\b(?:" + "|".join(map(re.escape, name.split())) + r")\b", re.I)
    email_rx = re.compile(re.escape(email), re.I)
    # The repository lives under the author's GitHub account (checks/site.json repo_url), so
    # its owner handle sits inside every repository href. Only that - an href attribute value
    # starting with the pinned repo_url, in the raw-source variant - is exempt from the stray
    # first/last-name count. The handle in visible text, a title/alt/aria attribute, PDF text
    # or any other URL still counts - including a sibling repository whose name merely starts
    # with the pinned one (".../filingstrail-notes"): the URL must end, or continue with / ? #,
    # right after the pinned repo_url.
    repo = (site.get("repo_url") or "").strip().rstrip("/")
    href_rx = re.compile(r"""\bhref\s*=\s*["']?""" + re.escape(repo)
                         + r"""(?:[/?#][^\s"'<>]*)?(?=["'\s>]|$)""", re.I) if repo else None
    exempted: dict[str, int] = {}

    name_counts, stray, email_raw, email_vis, mailtos, other_mailtos = {}, {}, {}, {}, {}, []
    for r in sorted(bundle.files):
        low = r.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2")):
            continue
        srch = bundle.searchable(r)
        nc = max((len(name_rx.findall(v)) for vs in srch.values() for v in vs), default=0)
        wc, wc_all = 0, 0
        for method, vs in srch.items():
            for v in vs:
                n_all = len(word_rx.findall(v))
                n = len(word_rx.findall(href_rx.sub(" ", v))) if (href_rx and method == "raw") else n_all
                wc, wc_all = max(wc, n), max(wc_all, n_all)
        if wc_all > wc:
            exempted[r] = wc_all - wc
        ec = max((len(email_rx.findall(v)) for vs in srch.values() for v in vs), default=0)
        if nc:
            name_counts[r] = nc
        if not low.endswith(".csv") and wc > 2 * nc:  # each full-name hit accounts for 2 words
            stray[r] = wc - 2 * nc
        if ec:
            email_raw[r] = ec
        if low.endswith((".html", ".htm")):
            pg = bundle.page(r)
            v = len(email_rx.findall(pg.text))
            if v:
                email_vis[r] = v
            for a in pg.nodes("a", "area", "link"):
                href = (a.get("href") or "").strip()
                if href.lower().startswith("mailto:"):
                    addr = urllib.parse.unquote(href[7:].split("?", 1)[0]).strip().lower()
                    if addr == email.lower():
                        mailtos[r] = mailtos.get(r, 0) + 1
                    else:
                        other_mailtos.append(f"{r}: {href}")

    total = sum(name_counts.values())
    f = []
    if total != 1:
        f.append(f'"{name}" appears {total} times (expected exactly 1): {name_counts}')
    if total and set(name_counts) != {root_rel}:
        f.append(f"appears outside {root_rel}: {sorted(set(name_counts) - {root_rel})}")
    if stray:
        f.append(f"stray first/last-name words (not part of the full name): {stray}")
    if root_rel in bundle.files:
        rp = bundle.page(root_rel)
        vis = len(name_rx.findall(rp.text))
        if vis != 1:
            f.append(f"{vis} occurrences in the root page's visible text (expected 1)")
        last = rp.blocks[-1] if rp.blocks else ""
        if not name_rx.search(last):
            f.append(f'not on the last line of the root page; last visible block is "{ascii_ctx(last, 100)}"')
    ex_note = ([f"owner-handle words exempted only inside href=\"{repo}...\" attribute values: "
                + ", ".join(f"{r} x{c}" for r, c in sorted(exempted.items()))] if exempted else [])
    (R.fail("C1", f'author name "{name}"', f + ex_note) if f else
     R.ok("C1", f'"{name}" appears exactly once across all dist text (HTML text+attributes, PDF text+metadata, '
                f'CSV, XML...), in {root_rel}, on the last line: "{ascii_ctx(bundle.page(root_rel).blocks[-1], 90)}"',
          ex_note))

    f = []
    if sum(email_vis.values()) != 1 or set(email_vis) != {root_rel}:
        f.append(f"visible-text occurrences {email_vis or 0} (expected exactly 1, on {root_rel})")
    if sum(mailtos.values()) != 1 or set(mailtos) != {root_rel}:
        f.append(f"mailto links to it {mailtos or 0} (expected exactly 1, on {root_rel})")
    if other_mailtos:
        f.append(f"other mailto links: {other_mailtos}")
    expected_raw = {root_rel: 2}
    extra = {r: c for r, c in email_raw.items() if expected_raw.get(r) != c}
    if extra:
        f.append(f"raw occurrences (entity/URL-decoded, all attributes, PDFs, CSV...) {email_raw}; "
                 f"expected {expected_raw} (1 visible + 1 mailto href)")
    (R.fail("C2", f"email {email}", f) if f else
     R.ok("C2", f"email {email}: 1 visible occurrence + 1 mailto link, both on {root_rel}; nowhere else"))


# ================================================================= check D

def page_subresources(pg: Page, bundle: Bundle, analytics_path: str):
    """[(url, kind)] of same-origin subresources a page loads (plus external ones)."""
    refs = []
    for n in pg.root.iter():
        t = n.tag
        if t == "link":
            rels = (n.get("rel") or "").lower().split()
            if set(rels) & {"stylesheet", "icon", "shortcut", "apple-touch-icon", "preload", "modulepreload",
                            "manifest", "mask-icon", "prefetch"} and n.get("href"):
                refs.append((n.get("href"), "link:" + "/".join(rels)))
        elif t in ("script", "img", "source", "audio", "video", "track", "embed", "iframe", "input"):
            if n.get("src"):
                refs.append((n.get("src"), t))
            if n.get("srcset"):
                refs += [(u, t + "[srcset]") for u in srcset_urls(n.get("srcset"))]
            if n.get("poster"):
                refs.append((n.get("poster"), t + "[poster]"))
        elif t in ("image", "feimage"):
            h = n.get("href") or n.get("xlink:href")
            if h and not h.startswith("#"):
                refs.append((h, t))
        st = n.get("style")
        if st:
            refs += [(m.group(2), "style url()") for m in _CSS_URL.finditer(st) if not m.group(2).startswith("#")]
        if t == "style":
            refs += [(m.group(2), "<style> url()") for m in _CSS_URL.finditer(n.text())
                     if not m.group(2).startswith("#")]
    return refs


def check_D(bundle: Bundle, site: dict, R: Report) -> None:
    R.section("D. PAGE WEIGHT - HTML + same-origin subresources + analytics allowance")
    limit = site.get("page_weight_limit_bytes", 300000)
    allowance = site.get("analytics_allowance_bytes", 6000)
    ap = site.get("analytics_path", "/_vercel/insights/script.js")
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        items = [(rel, len(pg.raw), len(gzip.compress(pg.raw, 9)))]
        seen = {rel}
        queue = page_subresources(pg, bundle, ap)
        notes = []
        while queue:
            u, kind = queue.pop(0)
            if u.startswith("data:") or u.split("#")[0] == ap:
                continue
            if url_host(u) is not None and url_host(u) not in site.get("hosts", []):
                notes.append(f"external subresource (not counted, see E): {u}")
                continue
            path = urllib.parse.urlsplit(u).path if url_host(u) else u
            r2 = resolve_path(path, bundle.files) if path.startswith("/") else None
            if r2 is None:
                notes.append(f"unresolved subresource (see G): {u}")
                continue
            if r2 in seen:
                continue
            seen.add(r2)
            data = bundle.files[r2]
            items.append((r2, len(data), len(gzip.compress(data, 9))))
            if r2.endswith(".css"):
                for m in _CSS_URL.finditer(_CSS_COMMENT.sub("", data.decode("utf-8", "replace"))):
                    if not m.group(2).startswith(("#", "data:")):
                        queue.append((m.group(2), "css url()"))
        if "favicon.ico" in bundle.files and "favicon.ico" not in seen:
            d = bundle.files["favicon.ico"]
            items.append(("favicon.ico (implicit request)", len(d), len(gzip.compress(d, 9))))
        raw = sum(i[1] for i in items) + allowance
        gz = sum(i[2] for i in items) + allowance
        det = [f"{r}: {b:,} B raw / {g:,} B gzip" for r, b, g in items] + \
              [f"{ap}: {allowance:,} B allowance (served by Vercel)"] + notes
        msg = f"{rel}: {raw:,} B raw / {gz:,} B gzip (limit {limit:,} raw)"
        (R.fail("D", msg, det) if raw >= limit else R.ok("D", msg, det))


# ================================================================= check E (static)

REF_TAGS = ("script", "link", "img", "source", "iframe", "embed", "object", "video", "audio", "track",
            "input", "image", "use", "feimage", "frame")


def page_css(bundle: Bundle, pg: Page):
    """[(origin, css_text)] that actually applies to one page: its <style> elements plus the
    same-origin stylesheets it links."""
    out = []
    for n in pg.root.iter():
        if n.tag == "style":
            out.append((f"{pg.rel} <style>", n.text()))
        elif n.tag == "link" and "stylesheet" in (n.get("rel") or "").lower().split() and n.get("href"):
            r = resolve_path(n.get("href"), bundle.files) if n.get("href").startswith("/") else None
            if r:
                out.append((r, bundle.files[r].decode("utf-8", "replace")))
    return out


def all_css(bundle: Bundle):
    """[(origin, css_text)] from .css files, <style> elements and style attributes."""
    out = []
    for r in bundle.files:
        if r.lower().endswith(".css"):
            out.append((r, bundle.files[r].decode("utf-8", "replace")))
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        for n in pg.root.iter():
            if n.tag == "style":
                out.append((f"{rel} <style>", n.text()))
            if n.get("style"):
                out.append((f"{rel} <{n.tag} style>", n.get("style")))
    return out


def check_E_static(bundle: Bundle, site: dict, R: Report) -> None:
    R.section("E. NO-JS AND NO THIRD PARTY - static")
    tag = site["analytics_tag"]
    ap = site["analytics_path"]
    hosts = set(site.get("hosts", []))
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        f = []
        scripts = pg.nodes("script")
        n_script_tags = len(re.findall(r"<script\b", pg.src, re.I))
        if pg.src.count(tag) != 1:
            f.append(f"exact tag {tag} appears {pg.src.count(tag)} times (expected 1)")
        if n_script_tags != 1 or len(scripts) != 1:
            f.append(f"{n_script_tags} <script> tags in source ({len(scripts)} parsed); expected exactly 1")
        for s in scripts:
            a = s.attrs
            if s.get("src") is None:
                f.append(f"inline script (type={s.get('type')!r}): {ascii_ctx(s.text().strip(), 80)!r}")
            elif s.get("src") != ap or set(a) != {"defer", "src"} or s.text().strip():
                f.append(f"non-analytics or altered script: {a}")
            if s.parent is None or s.parent.tag != "head":
                f.append(f"script not in <head> (parent <{s.parent.tag if s.parent else None}>)")
        for n in pg.root.iter():
            for k, v in n.attrs.items():
                if k.startswith("on"):
                    f.append(f"event-handler attribute {k}= on <{n.desc()}>")
                if k in ("href", "src", "action", "formaction", "xlink:href") and v and \
                        v.strip().lower().startswith(("javascript:", "vbscript:")):
                    f.append(f"{k}={v[:40]!r} on <{n.desc()}>")
            if n.tag in ("noscript", "iframe", "object", "embed", "frame", "frameset", "applet", "portal"):
                f.append(f"<{n.tag}> element present")
            if n.tag == "base":
                f.append(f"<base href={n.get('href')!r}> present")
            if n.tag == "meta" and (n.get("http-equiv") or "").lower() == "refresh":
                f.append("<meta http-equiv=refresh> present")
            in_svg_defs = any(a.tag in SVG_NONVISIBLE for a in n.ancestors())
            if "hidden" in n.attrs and not in_svg_defs:
                f.append(f"hidden attribute on <{n.desc()}>")
            st = (n.get("style") or "").lower().replace(" ", "")
            if not in_svg_defs and ("display:none" in st or "visibility:hidden" in st):
                f.append(f"inline {'display:none' if 'display:none' in st else 'visibility:hidden'} on <{n.desc()}>")
            if n.tag in REF_TAGS:
                for k in ("src", "href", "srcset", "data", "poster", "xlink:href"):
                    v = n.get(k)
                    if not v:
                        continue
                    for u in (srcset_urls(v) if k == "srcset" else [v]):
                        h = url_host(u)
                        if h is not None and h not in hosts:
                            f.append(f"<{n.tag} {k}={u!r}> points to another host ({h})")
        details = [x for x in dict.fromkeys(f)]
        (R.fail("E1", f"{rel}: scripts / handlers / embeds / hidden / third-party refs", details[:15]) if details
         else R.ok("E1", f"{rel}: only script is {tag} in <head>; no inline JS, on* handlers, noscript, "
                         f"iframe/object/embed, hidden elements or third-party src/href"))
        det = [d for d in (n for n in pg.root.iter() if n.tag == "details") if "open" not in d.attrs]
        if det:
            R.warn("E1", f"{rel}: {len(det)} closed <details> (content collapsed by default, works without JS)")
        css_hidden = []
        for origin, css in page_css(bundle, pg):
            for sel, body, ctx in css_rules(css):
                if sel.startswith("@") or any("print" in c.lower() for c in ctx):
                    continue
                d = {k: v.replace(" ", "").lower() for k, v in css_decls(body).items()}
                if d.get("display", "").startswith("none") or d.get("visibility", "").startswith("hidden"):
                    keys = selector_keys(sel)
                    if any(("." + c) in keys or n.tag in keys for n in pg.root.iter() for c in n.classes() | {""}):
                        css_hidden.append(sel.strip()[:60])
        if css_hidden:
            R.info("E1", f"{rel}: hidden on screen by stylesheet rules (not by attribute/inline style; "
                         f"check they are intentional): {sorted(set(css_hidden))}")
    # CSS
    f = []
    for origin, css in all_css(bundle):
        c = _CSS_COMMENT.sub("", css)
        if re.search(r"@import\b", c, re.I):
            f.append(f"@import in {origin}")
        if re.search(r"@font-face\b", c, re.I):
            f.append(f"@font-face in {origin}")
        for m in _CSS_URL.finditer(c):
            h = url_host(m.group(2))
            if h is not None and h not in hosts:
                f.append(f"url({m.group(2)}) in {origin}")
    (R.fail("E2", "CSS: @import / @font-face / remote url()", f[:12]) if f
     else R.ok("E2", "CSS: no @import, no @font-face, no url(http...) in .css files, <style> or style attributes"))
    # cookies / storage / stray JS
    f, prose = [], []
    api = re.compile(r"localStorage|sessionStorage|document\s*\.\s*cookie|cookieStore|indexedDB|set-cookie", re.I)
    for r, data in bundle.files.items():
        low = r.lower()
        if low.endswith((".js", ".mjs")):
            f.append(f"JavaScript file in dist: {r}")
        if not low.endswith((".html", ".htm", ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".svg", ".webmanifest")):
            continue
        s = data.decode("utf-8", "replace")
        for m in api.finditer(s):
            f.append(f'{r}: "{ctx_around(s, m.start(), m.end(), 30)}"')
        if low.endswith((".html", ".htm")) and re.search(r"cookie", bundle.page(r).text, re.I):
            prose.append(r)
    (R.fail("E3", "cookie / storage usage or JS files", f[:12]) if f
     else R.ok("E3", "no localStorage/sessionStorage/document.cookie/indexedDB/Set-Cookie; no .js files in dist"))
    if prose:
        R.info("E3", f"the word 'cookie' appears in visible prose (fine if it says 'cookieless'): {prose}")


# ================================================================= check F (static)

def chart_label(svg: Node) -> str:
    lines = svg_lines(svg)
    return (max(lines, key=len) if lines else "?")[:50]


def check_F_static(bundle: Bundle, R: Report) -> None:
    R.section("F. MOBILE - static (CSS evaluated per page: its <style> + linked stylesheets)")
    hidden_root = []
    xscroll_keys: set[str] = set()
    origin_of: dict[str, str] = {}

    def load_rules(pg: Page):
        xscroll_keys.clear()
        origin_of.clear()
        for origin, css in page_css(bundle, pg):
            for sel, body, ctx in css_rules(css):
                if sel.startswith("@") or any("print" in c.lower() and "screen" not in c.lower() for c in ctx):
                    continue
                d = css_decls(body)
                if decl_gives_xscroll(d):
                    for k in selector_keys(sel):
                        xscroll_keys.add(k)
                        origin_of.setdefault(k, f"{origin}: {sel.strip()[:60]} {{overflow-x:auto}}")
                ox = (d.get("overflow-x") or d.get("overflow") or "").split()
                if ox and ox[0] in ("hidden", "clip") and ({"html", "body"} & selector_keys(sel)):
                    hidden_root.append(f"{origin}: {sel.strip()} {{overflow-x:{ox[0]}}}")

    def container(n: Node):
        for a in n.ancestors():
            st = (a.get("style") or "").lower().replace(" ", "")
            if re.search(r"overflow(-x)?:(auto|scroll)", st):
                return f"<{a.desc()}> inline style"
            for c in a.classes():
                if "." + c in xscroll_keys:
                    return f"<{a.desc()}> via {origin_of['.' + c]}"
            if a.tag in xscroll_keys:
                return f"<{a.desc()}> via {origin_of[a.tag]}"
        return None

    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        load_rules(pg)
        f, ok = [], []
        vp = [n.get("content") or "" for n in pg.nodes("meta") if (n.get("name") or "").lower() == "viewport"]
        if not vp or "width=device-width" not in vp[0].replace(" ", ""):
            f.append(f"missing <meta name=viewport content=width=device-width...> (got {vp})")
        for t in pg.nodes("table"):
            c = container(t)
            first = table_rows(t)[:1]
            label = first[0][1][0] if first and first[0][1] else "?"
            (ok if c else f).append(f'<table> "{label}" ' + (f"inside {c}" if c else
                                                             "NOT inside an overflow-x:auto container"))
        for s in svg_top(pg.root):
            if not any(n.tag == "text" for n in s.iter()):
                continue  # icon, not a chart
            c = container(s)
            (ok if c else f).append(f'chart <svg> "{chart_label(s)}" ' + (f"inside {c}" if c else
                                                                          "NOT inside a scroll container"))
        (R.fail("F1", f"{rel}: viewport meta, tables and charts in scroll containers", f + ok) if f
         else R.ok("F1", f"{rel}: viewport meta present; {len(ok)} table/chart element(s) in scroll containers", ok))
    if hidden_root:
        R.warn("F1", "overflow-x hidden/clip on html/body can mask horizontal overflow",
               list(dict.fromkeys(hidden_root)))


# ================================================================= check G

def check_G(bundle: Bundle, site: dict, R: Report) -> None:
    R.section("G. LINKS, IDS, DEPLOY FILES")
    hosts = set(site.get("hosts", []))
    ap = site["analytics_path"]
    external: dict[str, set] = {}
    ids_by_page = {rel: Counter(n.get("id") for n in bundle.page(rel).root.iter() if n.get("id"))
                   for rel in bundle.html_rels()}
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        f, warn = [], []
        refs = []
        for n in pg.root.iter():
            for k, v in n.attrs.items():
                if v is None:
                    continue
                if k in ("href", "src", "poster", "data", "xlink:href", "action", "formaction", "cite", "background"):
                    refs.append((n, k, v.strip()))
                elif k == "srcset":
                    refs += [(n, k, u) for u in srcset_urls(v)]
                elif "url(" in v and k != "content":
                    refs += [(n, k, m.group(2)) for m in _CSS_URL.finditer(v)]
            if n.tag == "style":
                refs += [(n, "<style>", m.group(2)) for m in _CSS_URL.finditer(n.text())]
        n_int = 0
        for n, k, u in refs:
            if n.tag == "meta":
                continue
            low = u.lower()
            if low.startswith(("mailto:", "tel:", "data:")):
                continue
            if u == "" or low in ("none", "null", "undefined", "#"):
                (warn if u == "#" else f).append(f'placeholder {k}="{u}" on <{n.desc()}>')
                continue
            h = url_host(u)
            if h is not None:
                if h in hosts:
                    p = urllib.parse.urlsplit(u).path or "/"
                    if resolve_path(p, bundle.files) is None:
                        f.append(f"absolute same-site URL does not resolve in dist: {u}")
                else:
                    external.setdefault(h, set()).add(u)
                continue
            if u.startswith("#"):
                frag = urllib.parse.unquote(u[1:])
                if frag and frag not in ids_by_page[rel]:
                    f.append(f'fragment {u} on <{n.desc()}> has no id="{frag}" on this page')
                continue
            if not u.startswith("/"):
                f.append(f'relative URL {k}="{u}" on <{n.desc()}> (must be root-absolute)')
                continue
            if u.split("#")[0].split("?")[0] == ap:
                continue
            n_int += 1
            target = resolve_path(u, bundle.files)
            if target is None:
                f.append(f'{k}="{u}" on <{n.desc()}> does not resolve to a file in dist')
                continue
            path = u.split("#")[0].split("?")[0]
            if path.endswith(".html") or (path.endswith("/") and path != "/"):
                warn.append(f'{k}="{u}" is not a clean URL (cleanUrls will redirect)')
            if "#" in u:
                frag = urllib.parse.unquote(u.split("#", 1)[1])
                if frag and target.endswith(".html") and frag not in ids_by_page.get(target, {}):
                    f.append(f'{u}: no id="{frag}" on {target}')
        dups = sorted(i for i, c in ids_by_page[rel].items() if c > 1)
        if dups:
            f.append(f"duplicate id attributes: {dups[:15]}" + (f" (+{len(dups) - 15} more)" if len(dups) > 15 else ""))
        for n in pg.root.iter():
            for k in ("aria-labelledby", "aria-describedby", "aria-controls", "aria-owns"):
                for ref in (n.get(k) or "").split():
                    if ref not in ids_by_page[rel]:
                        f.append(f'{k}="{ref}" on <{n.desc()}> has no target id')
            if n.tag == "label" and n.get("for") and n.get("for") not in ids_by_page[rel]:
                f.append(f'<label for="{n.get("for")}"> has no target id')
        (R.fail("G1", f"{rel}: links and ids", f[:20]) if f else
         R.ok("G1", f"{rel}: {n_int} internal root-absolute refs resolve (cleanUrls); no relative links; "
                    f"{len(ids_by_page[rel])} ids unique; aria/fragment targets exist"))
        if warn:
            R.warn("G1", f"{rel}: link style", warn[:8])
    if external:
        R.info("G2", "external links (listed, not fetched)",
               [f"{h}: {sorted(us)[0]}" + (f" (+{len(us) - 1} more)" if len(us) > 1 else "") for h, us in sorted(external.items())])
    else:
        R.info("G2", "no external links")
    # deploy files
    f, ok = [], []
    for need in ("index.html", "404.html", "robots.txt", "sitemap.xml", "vercel.json"):
        (ok if need in bundle.files else f).append(f"{need} {'present' if need in bundle.files else 'MISSING'}")
    if "vercel.json" in bundle.files:
        try:
            vj = json.loads(bundle.files["vercel.json"].decode("utf-8"))
            if vj.get("cleanUrls") is not True:
                f.append(f"vercel.json cleanUrls is {vj.get('cleanUrls')!r} (must be true: links are '/adv')")
            else:
                ok.append("vercel.json cleanUrls: true")
            if vj.get("trailingSlash") is True:
                f.append("vercel.json trailingSlash: true contradicts slash-less URLs")
            if vj.get("buildCommand") not in (None, "") or vj.get("framework") not in (None,):
                ok.append(f"vercel.json buildCommand={vj.get('buildCommand')!r} framework={vj.get('framework')!r}")
            if re.search(r"set-cookie", json.dumps(vj), re.I):
                f.append("vercel.json sets a cookie header")
        except Exception as e:  # noqa
            f.append(f"vercel.json does not parse: {e}")
    if "sitemap.xml" in bundle.files:
        sm = bundle.files["sitemap.xml"].decode("utf-8", "replace")
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", sm, re.S)
        base = site["base_url"].rstrip("/")
        if not locs:
            f.append("sitemap.xml has no <loc>")
        for loc in locs:
            loc = html.unescape(loc)
            if not (loc == base or loc.startswith(base + "/")):
                f.append(f"sitemap <loc> not under {base}: {loc}")
            elif resolve_path(loc[len(base):] or "/", bundle.files) is None:
                f.append(f"sitemap <loc> does not resolve in dist: {loc}")
        if locs:
            ok.append(f"sitemap.xml: {len(locs)} <loc> all under {base} and resolvable")
        pages = {rel_to_url(r) for r in bundle.html_rels() if r != "404.html"}
        missing = sorted(p for p in pages if base + (p if p != "/" else "/") not in locs and base + p not in locs)
        if missing:
            R.warn("G3", f"pages not in sitemap.xml: {missing}")
    if "robots.txt" in bundle.files:
        rb = bundle.files["robots.txt"].decode("utf-8", "replace")
        if re.search(r"^\s*Disallow:\s*/\s*$", rb, re.M | re.I):
            R.warn("G3", "robots.txt disallows the whole site")
        if "sitemap" not in rb.lower():
            R.warn("G3", "robots.txt does not reference the sitemap")
    (R.fail("G3", "deploy files", f + ok) if f else R.ok("G3", "deploy files", ok))


# ================================================================= check H / I

def find_key(d, key):
    if isinstance(d, dict):
        if key in d:
            return True, d[key]
        for v in d.values():
            found, val = find_key(v, key)
            if found:
                return True, val
    elif isinstance(d, list):
        for v in d:
            found, val = find_key(v, key)
            if found:
                return True, val
    return False, None


def check_H(bundle: Bundle, works: list[dict], site: dict, site_json: Path, allow_missing: bool, R: Report) -> None:
    R.section("H. REPOSITORY LINK")
    if not site_json.exists():
        R.fail("H", f"site config {site_json} not found - repo_url not set")
        return
    try:
        cfg = json.loads(site_json.read_text(encoding="utf-8"))
    except Exception as e:  # noqa
        R.fail("H", f"{site_json} does not parse: {e}")
        return
    mism = []
    for k in ("author", "email", "base_url", "name"):
        found, v = find_key(cfg, k)
        if found and v != site.get(k):
            mism.append(f"site.json {k}={v!r} but checks/site.json expects {site.get(k)!r}")
    if mism:
        R.warn("H", "site.json disagrees with checks/site.json", mism)
    found, repo = find_key(cfg, "repo_url")
    if not repo:
        msg = f"repo_url not set in {site_json.name} (value: {repo!r})"
        if allow_missing:
            R.warn("H", msg + " - allowed by --allow-missing-repo; DO NOT DEPLOY THE FINAL BUILD LIKE THIS")
        else:
            R.fail("H", msg + " - set it, or pass --allow-missing-repo for a preview build")
        for w in works:
            if w["page"] in bundle.files:
                pg = bundle.page(w["page"])
                bad = [a.get("href") for a in pg.nodes("a") if (a.get("href") or "").strip().lower()
                       in ("", "none", "null", "#", "undefined")]
                if bad:
                    R.fail("H", f"{w['page']}: placeholder repository links {bad}")
        return
    if not re.match(r"^https://[\w.-]+/\S+$", repo):
        R.fail("H", f"repo_url {repo!r} is not an https URL")
        return
    canon = lambda u: re.sub(r"\.git$", "", (u or "").strip().rstrip("/"))  # noqa  (build.py strips the same)
    pinned = site.get("repo_url")
    if pinned and canon(repo) != canon(pinned):
        R.fail("H", f"site.json repo_url {repo!r} != the repository checks/site.json pins ({pinned!r})")
    elif pinned:
        R.ok("H", f"site.json repo_url == pinned {pinned}")
    for w in works:
        rel = w["page"]
        if rel not in bundle.files:
            R.fail("H", f"{rel} missing")
            continue
        pg = bundle.page(rel)
        top = [n for n in pg.before_first("h2") if n.tag == "a"]
        norm_u = lambda u: (u or "").strip().rstrip("/")  # noqa
        hit = [a for a in top if norm_u(a.get("href")) == norm_u(repo)]
        anywhere = [a for a in pg.nodes("a") if norm_u(a.get("href")) == norm_u(repo)]
        if hit:
            R.ok("H", f'{rel}: repository link {repo} before the first <h2> (text "{norm(hit[0].text())}")')
        else:
            R.fail("H", f"{rel}: no link to repo_url {repo} before the first <h2>"
                        + (f" ({len(anywhere)} link(s) further down)" if anywhere else ""))


def check_I(bundle: Bundle, works: list[dict], R: Report, out_dir: Path | None) -> None:
    R.section("I. PDF LINK AND VALIDITY")
    for w in works:
        rel = w["page"]
        pdfs = sorted(r for r in bundle.files if fnmatch.fnmatch(r, w.get("pdf_glob", "")) and r.lower().endswith(".pdf"))
        if len(pdfs) != 1:
            R.fail("I", f"[{w['work']}] expected exactly 1 PDF matching {w.get('pdf_glob')}, found {pdfs}")
            if not pdfs:
                continue
        prel = pdfs[0]
        if rel in bundle.files:
            pg = bundle.page(rel)
            top = [n for n in pg.before_first("h2") if n.tag == "a" and n.get("href")]
            hit = [a for a in top if a.get("href").startswith("/") and resolve_path(a.get("href"), bundle.files) == prel]
            rel_hits = [a.get("href") for a in top if a.get("href").lower().endswith(".pdf") and not a.get("href").startswith("/")]
            if hit:
                R.ok("I1", f'{rel}: links {hit[0].get("href")} before the first <h2> (text "{norm(hit[0].text())}")')
            else:
                R.fail("I1", f"{rel}: no root-absolute link to /{prel} before the first <h2>"
                             + (f"; relative PDF links found: {rel_hits}" if rel_hits else ""))
        data = bundle.files[prel]
        px = bundle.pdf(prel)
        f = []
        if not data.startswith(b"%PDF-"):
            f.append("does not start with %PDF-")
        if b"%%EOF" not in data[-2048:]:
            f.append("no %%EOF trailer near the end (truncated?)")
        if not px["ok"]:
            f.append("PyMuPDF cannot open it: " + "; ".join(px["errors"]))
        if px["encrypted"]:
            f.append("encrypted")
        if px["pages"] < 1:
            f.append("0 pages")
        if px["pypdf_pages"] != px["pages"]:
            f.append(f"pypdf sees {px['pypdf_pages']} pages vs PyMuPDF {px['pages']} ({px['errors']})")
        if len(px["pymupdf"].strip()) < 2000:
            f.append(f"only {len(px['pymupdf'].strip())} chars of text (image-only or truncated?)")
        desc = f"{prel}: {len(data):,} B, {px['pages']} pages, {len(px['pymupdf']):,} chars text, meta: {ascii_ctx(px['meta'], 120)}"
        (R.fail("I2", desc, f) if f else R.ok("I2", desc))
        if out_dir and px["ok"]:
            try:
                fitz = _fitz()
                doc = fitz.open(stream=data, filetype="pdf")
                for i in range(min(2, doc.page_count)):
                    doc[i].get_pixmap(dpi=60).save(str(out_dir / f"pdf-{w['work']}-p{i + 1}.png"))
                doc.close()
                R.out(f"             page previews: {out_dir / ('pdf-' + w['work'] + '-p1.png')} (and p2)")
            except Exception as e:  # noqa
                R.warn("I2", f"could not render a preview: {e}")


# ================================================================= check J / K

def check_J(bundle: Bundle, works: list[dict], R: Report) -> None:
    R.section("J. FIRM-LEVEL CSV")
    for w in works:
        spec = w.get("csv")
        if not spec:
            continue
        rel = spec["path"]
        if rel not in bundle.files:
            R.fail("J", f"{rel} missing from dist")
            continue
        f, ok = [], []
        try:
            text = bundle.files[rel].decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception as e:  # noqa
            R.fail("J", f"{rel} does not parse as UTF-8 CSV: {e}")
            continue
        cols = list(rows[0].keys()) if rows else []
        if len(rows) != spec["rows_total"]:
            f.append(f"{len(rows):,} data rows, expected {spec['rows_total']:,} (gate: entered)")
        else:
            ok.append(f"{len(rows):,} rows = entered")
        truthy = lambda v: str(v).strip().lower() in ("true", "1", "yes", "y", "t")  # noqa
        fc = spec.get("flag_column")
        if fc:
            if fc not in cols:
                f.append(f"column {fc} missing (columns: {cols})")
            else:
                flagged = [r for r in rows if truthy(r[fc])]
                if len(flagged) != spec["flag_true"]:
                    f.append(f"{fc} true for {len(flagged):,}, expected {spec['flag_true']:,} (gate: validated)")
                else:
                    ok.append(f"{fc} true for {len(flagged):,} = validated")
                wc = spec.get("website_column")
                if wc and wc in cols and flagged:
                    rate = f"{sum(1 for r in flagged if not truthy(r[wc])) / len(flagged) * 100:.1f}"
                    if rate != spec["no_website_rate_among_flagged"]:
                        f.append(f"no-website rate among {fc} rows is {rate}%, expected {spec['no_website_rate_among_flagged']}%")
                    else:
                        ok.append(f"no-website rate among validated = {rate}%")
                elif wc:
                    f.append(f"column {wc} missing")
        if w["page"] in bundle.files:
            hrefs = {a.get("href") for a in bundle.page(w["page"]).nodes("a")}
            if spec.get("linked_from_page") and spec["linked_from_page"] not in hrefs:
                f.append(f'{w["page"]} has no <a href="{spec["linked_from_page"]}">')
            else:
                ok.append(f"linked from {w['url']}")
        (R.fail("J", f"{rel}", f + ok) if f else R.ok("J", f"{rel}: " + "; ".join(ok)))


def check_K(bundle: Bundle, works: list[dict], site: dict, R: Report) -> None:
    R.section("K. CHART COLOURS (CSS variable interface; WARN only)")
    spec = site.get("chart_css_vars", {})
    for w in works:
        if w["page"] not in bundle.files:
            continue
        pg = bundle.page(w["page"])
        light, dark, prnt = {}, {}, {}
        for origin, css in page_css(bundle, pg):
            for sel, body, ctx in css_rules(css):
                c = " ".join(ctx).lower()
                for k, v in css_decls(body).items():
                    if k.startswith("--chart-"):
                        # A print-only palette is its own medium, not the light theme:
                        # it is reported below, and K2 measures what it prints.
                        if "print" in c and "screen" not in c:
                            prnt[k] = v.lower()
                        else:
                            (dark if is_dark_ctx(sel, ctx) else light)[k] = v.lower()
        charts = [s for s in svg_top(pg.root) if any(n.tag == "text" for n in s.iter())]
        used, nofallback, white = set(), set(), 0
        for s in charts:
            for n in s.iter():
                for k, v in n.attrs.items():
                    if not v:
                        continue
                    for m in re.finditer(r"var\(\s*(--[\w-]+)\s*(,)?", v):
                        used.add(m.group(1))
                        if not m.group(2):
                            nofallback.add(m.group(1))
                    if re.search(r"(?:fill|background)\s*:\s*(#fff(?:fff)?|white)\b", v, re.I) or \
                            (k == "fill" and v.strip().lower() in ("#fff", "#ffffff", "white")):
                        white += 1
        warn, ok = [], []
        if charts and not used:
            warn.append("inline charts use no CSS variables: colours will not adapt to dark mode")
        for v in sorted(used):
            if v not in light:
                warn.append(f"{v} used by a chart but not defined in the light palette")
            if v not in dark:
                warn.append(f"{v} used by a chart but not redefined for dark mode")
            if v in spec and light.get(v) and light[v] != spec[v][0]:
                warn.append(f"{v} light value {light[v]} != interface {spec[v][0]}")
            if v in spec and dark.get(v) and dark[v] != spec[v][1]:
                warn.append(f"{v} dark value {dark[v]} != interface {spec[v][1]}")
        if nofallback:
            warn.append(f"var() without a literal fallback: {sorted(nofallback)}")
        if white:
            warn.append(f"{white} opaque white fill(s) left in chart SVGs (background patch not removed?)")
        ok.append(f"{len(charts)} charts, variables used: {sorted(used)}")
        diff = {k: v for k, v in prnt.items() if k in used and v != light.get(k)}
        if diff:
            ok.append("print palette differs from light: " + ", ".join(f"{k} {v} (light {light.get(k)})"
                                                                     for k, v in sorted(diff.items())))
        (R.warn("K", w["page"], warn + ok) if warn else R.ok("K", f"{w['page']}: " + "; ".join(ok)
                                                               + "; all defined for light and dark"))


# ======================================================= site-wide contract
#
#   N1 masthead on every page       N2 root work list        N3 footers
#   N4 masthead/footer hidden in print (CSS + the PDF text)   N5 sitemap
#   M1 /method structure, intro and METHOD.md extraction verbatim
#   M2 /method numeric provenance   M3 /method churn figures vs build/adv/report_stats.log
#   D1 /data tables == archive/MANIFEST.csv   D2 panel files agree across /data, /adv, METHOD.md
#   D3 stale daggers == stale rule  D4 dist/data/manifest.csv == archive/MANIFEST.csv
#   K2 chart-label halo             H2 repository links name real files on the default branch
#
# Expectations: checks/pages.json. Inputs read (never written): content/*.md,
# adv-report/METHOD.md, adv-report/REPORT.md, archive/MANIFEST.csv, build/adv/report_stats.log.

class Sources:
    """Read-only inputs the page checks compare against."""

    def __init__(self, repo: Path, content: Path, stats_log: Path):
        self.repo, self.content, self.stats_log = repo, content, stats_log

    def text(self, rel: str) -> str:
        return (self.repo / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def load_pages(checks_dir: Path) -> dict | None:
    p = checks_dir / "pages.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def canon_url(u) -> str:
    return re.sub(r"\.git$", "", (u or "").strip().rstrip("/"))


# ---- Markdown, read independently of build.py (python-markdown is not a gate dependency)

_MD_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_MD_LINK = re.compile(r"!?\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^()\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_AUTO = re.compile(r"<((?:https?|mailto):[^>\s]+)>")


def _md_emph(s: str) -> str:
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"\1", s)
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", s)


def md_inline(s: str, links: list | None = None) -> str:
    """Markdown inline syntax -> the text a reader sees (code spans, links, autolinks,
    emphasis, escapes). (text, href) of every link is appended to `links`."""
    codes: list[str] = []

    def stash(m):
        codes.append(m.group(2).strip() or m.group(2))
        return f"\x00{len(codes) - 1}\x00"

    def restore(t: str) -> str:
        return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], t)

    s = _MD_CODE.sub(stash, s)
    found: list[tuple[str, str]] = []
    s = _MD_LINK.sub(lambda m: found.append((m.group(1), m.group(2))) or m.group(1), s)
    s = _MD_AUTO.sub(lambda m: found.append((m.group(1), m.group(1))) or m.group(1), s)
    if links is not None:
        links += [(norm(html.unescape(restore(_md_emph(t)))), h) for t, h in found]
    return html.unescape(restore(_md_emph(s)))


def md_blocks(text: str, links: list | None = None) -> list[tuple[str, object]]:
    """Markdown -> [(kind, value)]: kind h1..h6 / p / li / pre with normalized text, or
    ('table', [[cell, ...], ...]) with the separator row dropped."""
    out: list[tuple[str, object]] = []
    kind, cur, code, in_code, table = None, [], [], False, None

    def flush():
        nonlocal kind, table
        if cur:
            out.append((kind, norm(md_inline(" ".join(cur), links))))
            cur.clear()
        kind = None
        if table is not None:
            out.append(("table", table))
            table = None

    for ln in text.replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if in_code:
            if s.startswith("```"):
                out.append(("pre", norm("\n".join(code))))
                code.clear()
                in_code = False
            else:
                code.append(ln)
            continue
        if s.startswith("```"):
            flush()
            in_code = True
            continue
        if not s:
            flush()
            continue
        if s.startswith("|"):
            if table is None:
                flush()
                table = []
            inner = s[1:-1] if s.endswith("|") and len(s) > 1 else s[1:]
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", inner)]
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            table.append([norm(md_inline(c, links)) for c in cells])
            continue
        if table is not None:
            flush()
        m = re.match(r"(#{1,6})\s+(.*?)(?:\s+#+)?$", s)
        if m:
            flush()
            out.append((f"h{len(m.group(1))}", norm(md_inline(m.group(2), links))))
            continue
        m = re.match(r"(?:[-*+]|\d{1,3}[.)])\s+(.*)", s)
        if m:
            flush()
            kind = "li"
            cur.append(m.group(1))
            continue
        if kind is None:
            kind = "p"
        cur.append(s)
    if in_code:
        out.append(("pre", norm("\n".join(code))))
    flush()
    return out


def block_words(blocks) -> list[str]:
    w: list[str] = []
    for kind, v in blocks:
        if kind == "table":
            for row in v:
                w += " ".join(row).split()
        else:
            w += str(v).split()
    return w


def find_seq(hay: list[str], needle: list[str]) -> list[int]:
    n = len(needle)
    if not n:
        return []
    return [i for i in range(len(hay) - n + 1) if hay[i] == needle[0] and hay[i:i + n] == needle]


def seq_break(hay: list[str], needle: list[str], ctx: int = 7) -> str:
    """Where the longest partial alignment of `needle` inside `hay` breaks."""
    best_k, best_i = 0, -1
    for i, w in enumerate(hay):
        if w != needle[0]:
            continue
        k = 0
        while k < len(needle) and i + k < len(hay) and hay[i + k] == needle[k]:
            k += 1
        if k > best_k:
            best_k, best_i = k, i
    if best_i < 0:
        return f'the first expected word "{needle[0]}" never occurs on the page'
    k, i = best_k, best_i
    return (f'{k} of {len(needle)} words match, then expected "...{" ".join(needle[max(0, k - ctx):k + ctx])}..." '
            f'but the page has "...{" ".join(hay[max(0, i + k - ctx):i + k + ctx])}..."')


# ---- page helpers

def main_of(pg: Page) -> Node:
    return next((n for n in pg.root.iter() if n.tag == "main"), pg.root)


def page_level(n: Node) -> bool:
    return not any(a.tag in ("main", "article", "section", "aside", "nav") for a in n.ancestors())


def first_after(pg: Page, node: Node, tags, stop=("h2",)) -> Node | None:
    """First element with a tag in `tags` after `node` in document order, before the next `stop`."""
    seen = False
    inside = {id(x) for x in node.iter()}
    for n in pg.root.iter():
        if n is node:
            seen = True
            continue
        if not seen or id(n) in inside:
            continue
        if n.tag in stop:
            return None
        if n.tag in tags:
            return n
    return None


def table_grid(t: Node):
    """[(tr, [cell nodes], is_header_row)] in document order; nested tables skipped."""
    out = []

    def walk(n):
        for c in n.children:
            if c.tag is None or c.tag == "table":
                continue
            if c.tag == "tr":
                cells = [x for x in c.children if x.tag in ("td", "th")]
                out.append((c, cells, bool(cells) and all(x.tag == "th" for x in cells)))
            else:
                walk(c)

    walk(t)
    return out


def page_title(pg: Page) -> list[str]:
    return [norm(n.text()) for n in pg.root.iter() if n.tag == "title" and not any(a.tag == "svg" for a in n.ancestors())]


def page_description(pg: Page) -> list[str]:
    return [norm(n.get("content") or "") for n in pg.nodes("meta") if (n.get("name") or "").lower() == "description"]


def closest(target: str, pool: list[str]) -> str:
    import difflib
    m = difflib.get_close_matches(target, pool, n=1, cutoff=0.3)
    return ascii_ctx(m[0], 140) if m else "(nothing similar)"


# ================================================================= check N

def check_site_nav(bundle: Bundle, works: list[dict], site: dict, spec: dict, R: Report) -> None:
    R.section("N. MASTHEAD, ROOT WORK LIST, FOOTERS, PRINT, SITEMAP")
    ms, ft, rt = spec["masthead"], spec["footer"], spec["root"]
    root_rel = site.get("root_page", "index.html")
    exp_nav = [tuple(x) for x in ms["nav"]]
    current = {rel: (tuple(v["current"]) if v.get("current") else None) for rel, v in ms["pages"].items()}
    for w in works:
        current.setdefault(w["page"], ("Work", "true"))
    repo = canon_url(site.get("repo_url"))
    word_rx = re.compile(r"\b(?:" + "|".join(map(re.escape, site["author"].split())) + r")\b", re.I)

    # N1 masthead
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        f, ok = [], []
        order = {id(n): i for i, n in enumerate(pg.root.iter())}
        hs = [n for n in pg.root.iter() if n.tag == "header" and "masthead" in n.classes()]
        if any(n.tag == "header" and "site" in n.classes() for n in pg.root.iter()):
            f.append('the old <header class="site"> home link is still on the page')
        if len(hs) != 1:
            f.append(f'{len(hs)} <header class="masthead"> elements (expected exactly 1)')
        if hs:
            h = hs[0]
            mains = pg.nodes("main")
            if any(a.tag in ("main", "article", "footer") for a in h.ancestors()):
                f.append("masthead sits inside <main>/<article>/<footer>")
            elif mains and order[id(h)] > order[id(mains[0])]:
                f.append("masthead comes after <main>")
            navs = [n for n in h.iter() if n.tag == "nav"]
            in_nav = {id(x) for nv in navs for x in nv.iter()}
            outside = [a for a in h.iter() if a.tag == "a" and id(a) not in in_nav]
            wm = [a for a in outside if norm(a.text()) == ms["wordmark"]]
            if len(wm) != 1 or len(outside) != 1:
                f.append(f'masthead links outside the nav: {[(norm(a.text()), a.get("href")) for a in outside]} '
                         f'(expected exactly the wordmark "{ms["wordmark"]}")')
            if wm:
                a = wm[0]
                h1 = next((x for x in a.ancestors() if x.tag == "h1"), None)
                if a.get("href") != ms["wordmark_href"]:
                    f.append(f'wordmark href="{a.get("href")}" (expected "{ms["wordmark_href"]}")')
                if rel == root_rel:
                    h1s = pg.nodes("h1")
                    if h1 is None:
                        f.append("root: the wordmark is not inside the page's <h1>")
                    else:
                        if ms["root_h1_class"] not in h1.classes():
                            f.append(f'root: wordmark <h1> lacks class="{ms["root_h1_class"]}" (has {sorted(h1.classes())})')
                        if len(h1s) != 1:
                            f.append(f"root: {len(h1s)} <h1> elements; the wordmark must be the only one "
                                     f"(others: {[norm(x.text()) for x in h1s if x is not h1]})")
                    if a.get("aria-current") != "page":
                        f.append(f'root: wordmark aria-current={a.get("aria-current")!r} (expected "page")')
                    if not f:
                        ok.append(f'<h1 class="{ms["root_h1_class"]}"><a href="/" aria-current="page">'
                                  f'{ms["wordmark"]}</a></h1> is the only <h1>')
                else:
                    if h1 is not None:
                        f.append("the wordmark is inside an <h1> (only the root page may do that)")
                    if "aria-current" in a.attrs:
                        f.append(f'wordmark carries aria-current={a.get("aria-current")!r} off the root page')
                    if not f:
                        ok.append(f'wordmark "{ms["wordmark"]}" -> {ms["wordmark_href"]} (plain link)')
            if len(navs) != 1:
                f.append(f"{len(navs)} <nav> elements in the masthead (expected 1)")
            else:
                nv = navs[0]
                if nv.get("aria-label") != ms["nav_label"]:
                    f.append(f'nav aria-label={nv.get("aria-label")!r} (expected "{ms["nav_label"]}")')
                links = [a for a in nv.iter() if a.tag == "a"]
                got = [(norm(a.text()), a.get("href")) for a in links]
                if got != exp_nav:
                    f.append(f"nav links {got} != expected {exp_nav} (text, href, order)")
                cur = [(norm(a.text()), a.get("aria-current")) for a in links if "aria-current" in a.attrs]
                if rel in current:
                    want = [current[rel]] if current[rel] else []
                    if cur != want:
                        f.append(f"nav aria-current {cur or 'none'} (expected {want or 'none'})")
                    else:
                        ok.append(f"nav[aria-label={ms['nav_label']}] {' | '.join(t + ' -> ' + h for t, h in got)}; "
                                  f"aria-current: {', '.join(f'{t}={v}' for t, v in cur) or 'none'}")
                else:
                    if len(cur) > 1:
                        f.append(f"nav aria-current on {len(cur)} links: {cur}")
                    R.warn("N1", f"{rel}: no aria-current expectation in checks/pages.json for this page (got {cur or 'none'})")
        if rel == root_rel:
            tg = [n for n in pg.root.iter() if n.get("id") == rt["work_heading_id"]]
            if len(tg) != 1 or tg[0].tag != "h2" or norm(tg[0].text()) != rt["work_heading_text"]:
                f.append(f'root: "#{rt["work_heading_id"]}" target is {[(n.tag, norm(n.text())) for n in tg]} '
                         f'(expected one <h2 id="{rt["work_heading_id"]}">{rt["work_heading_text"]}</h2>)')
            else:
                ok.append(f'#{rt["work_heading_id"]} target: <h2 id="{rt["work_heading_id"]}">{rt["work_heading_text"]}</h2>')
        (R.fail("N1", f"{rel}: masthead", f + ok) if f else R.ok("N1", f"{rel}: masthead", ok))

    # N2 root work list, copy, footer
    if root_rel not in bundle.files:
        R.fail("N2", f"root page {root_rel} missing")
    else:
        rp = bundle.page(root_rel)
        f, ok = [], []
        blocks = visible_blocks(main_of(rp))
        exp_p = [norm(p) for p in rt["paragraphs"]]
        pos = []
        for p in exp_p:
            if p in blocks:
                pos.append(blocks.index(p))
            else:
                f.append(f'root copy changed: expected paragraph "{ascii_ctx(p, 90)}" - closest block: "{closest(p, blocks)}"')
        if pos and pos != sorted(pos):
            f.append(f"root paragraphs out of order (block positions {pos})")
        if not f:
            ok.append(f"{len(exp_p)} existing paragraphs verbatim, in order")
        first_p = next((n for n in main_of(rp).iter() if n.tag == "p"), None)
        if first_p is not None and exp_p and norm(first_p.text()) == exp_p[0]:
            sizes = [f"{sel.strip()[:40]} {{font-size:{css_decls(body)['font-size']}}}"
                     for _, css in page_css(bundle, rp) for sel, body, ctx in css_rules(css)
                     if "font-size" in css_decls(body) and not any("print" in c.lower() for c in ctx)
                     and any("." + c in selector_keys(sel) for c in first_p.classes())]
            if sizes:
                ok.append(f"lede: the first paragraph is styled {sizes[0]}")
            else:
                R.warn("N2", f"{root_rel}: cannot confirm the lede - no rule sets font-size on the first "
                             f"paragraph's class {sorted(first_p.classes()) or '(none)'}")
        elif exp_p:
            f.append("the first paragraph of the root page is not the first existing paragraph (the lede)")
        tg = [n for n in rp.root.iter() if n.get("id") == rt["work_heading_id"]]
        lst = first_after(rp, tg[0], ("ul", "ol")) if tg else None
        if lst is None:
            f.append(f'no <ul>/<ol> after the #{rt["work_heading_id"]} heading')
        else:
            items = [c for c in lst.children if c.tag == "li"]
            exp = rt["works"]
            label = norm(rt["in_progress_label"])
            if len(items) != len(exp):
                f.append(f"work list has {len(items)} items, expected {len(exp)}: "
                         f"{[ascii_ctx(norm(' '.join(visible_blocks(li))), 60) for li in items]}")
            for i, (li, w) in enumerate(zip(items, exp), 1):
                text = norm(" ".join(visible_blocks(li)))
                title = norm(w["title"])
                desc = norm(w["descriptor"]) if w.get("descriptor") else None
                anchors = [n for n in li.iter() if n.tag == "a"]
                g = []
                if title not in text:
                    g.append(f'title "{title}" missing')
                if desc and desc not in text:
                    g.append(f'descriptor "{desc}" missing')
                rest = text
                for piece in (title, desc, label if w.get("status") == "in_progress" else None):
                    if piece:
                        rest = rest.replace(piece, " ", 1)
                if w.get("status") == "in_progress":
                    if anchors:
                        g.append(f"in-progress item is linked: {[a.get('href') for a in anchors]}")
                    labels = [n for n in li.iter() if n is not li and n.tag in INLINE and norm(n.text()) == label]
                    if label not in text:
                        g.append(f'no "{label}" label')
                    elif len(labels) != 1:
                        g.append(f'"{label}" is not its own inline element (e.g. <span>) inside the item')
                    if re.search(r"\w", rest):
                        g.append(f'unexpected text beyond title/descriptor/label: "{norm(rest)}"')
                    if not g:
                        ok.append(f'item {i}: "{title}" unlinked, "{label}"' + (f', "{ascii_ctx(desc, 50)}"' if desc else ", no descriptor"))
                else:
                    if len(anchors) != 1 or anchors[0].get("href") != w["href"] or norm(anchors[0].text()) != title:
                        g.append(f'expected one link "{title}" -> {w["href"]}; got '
                                 f'{[(norm(a.text()), a.get("href")) for a in anchors]}')
                    if label in text:
                        g.append(f'a published work carries "{label}"')
                    if re.search(r"\w", rest):
                        g.append(f'unexpected text beyond title/descriptor: "{norm(rest)}"')
                    if not g:
                        ok.append(f'item {i}: "{ascii_ctx(title, 50)}" -> {w["href"]}')
                f += [f"item {i}: {x}" for x in g]
        foots = [n for n in rp.root.iter() if n.tag == "footer" and page_level(n)]
        ftxt = [norm(" ".join(visible_blocks(n))) for n in foots]
        if ftxt != [norm(ft["root_text"])]:
            f.append(f"root footer {ftxt} (expected exactly [{ft['root_text']!r}])")
        else:
            ok.append(f'root footer unchanged: "{ft["root_text"]}" (name-once: C1; figure budget: A-R)')
        (R.fail("N2", f"{root_rel}: copy, work list, footer", f + ok) if f
         else R.ok("N2", f"{root_rel}: copy, work list, footer", ok))

    # N3 footers on every other page
    for rel in bundle.html_rels():
        if rel == root_rel:
            continue
        pg = bundle.page(rel)
        f = []
        foots = [n for n in pg.root.iter() if n.tag == "footer" and page_level(n)]
        if len(foots) != 1:
            f.append(f"{len(foots)} page-level <footer> elements (expected 1)")
        for fo in foots[:1]:
            t = norm(" ".join(visible_blocks(fo)))
            if t != norm(ft["text"]):
                f.append(f'footer text "{t}" (expected "{ft["text"]}")')
            anchors = [n for n in fo.iter() if n.tag == "a"]
            got = [(norm(a.text()), canon_url(a.get("href"))) for a in anchors]
            if got != [(ft["link_text"], repo)]:
                f.append(f'footer links {got} (expected [("{ft["link_text"]}", "{repo}")])')
            attrs = " ".join(v or "" for n in fo.iter() for k, v in n.attrs.items() if k != "href")
            if word_rx.search(t) or word_rx.search(attrs):
                f.append("the author's name (or part of it) is in the footer")
        (R.fail("N3", f"{rel}: footer", f) if f else
         R.ok("N3", f'{rel}: footer "{ft["text"]}", link -> {repo}; no name'))

    # N4 masthead and footer hidden in print
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        keys = set()
        for origin, css in page_css(bundle, pg):
            for sel, body, ctx in css_rules(css):
                c = " ".join(ctx).lower()
                if "print" in c and "screen" not in c and \
                        css_decls(body).get("display", "").replace("!important", "").strip() == "none":
                    keys |= selector_keys(sel)
        targets = [n for n in pg.root.iter() if (n.tag == "header" and "masthead" in n.classes())
                   or (n.tag == "footer" and page_level(n))]
        shown = [n.desc() for n in targets if not (n.tag in keys or any("." + c in keys for c in n.classes()))]
        (R.fail("N4", f"{rel}: not hidden by an @media print display:none rule: {shown}") if shown else
         R.ok("N4", f"{rel}: {', '.join(sorted(n.desc() for n in targets)) or 'nothing'} hidden in print "
                    f"(@media print display:none on {sorted(k for k in keys if k in ('footer', 'header') or k.startswith('.'))[:6]})"))
    nav_rx = re.compile(r"\b" + r"\s+".join(re.escape(t) for t, _ in exp_nav) + r"\b")
    for w in works:
        pdfs = sorted(r for r in bundle.files if fnmatch.fnmatch(r, w.get("pdf_glob", "")) and r.lower().endswith(".pdf"))
        for prel in pdfs:
            vs = text_variants(bundle.pdf(prel)["pymupdf"])
            f = [f'"{s}" in the PDF text' for s in (ft["text"], ft["link_text"], "Built from public SEC filings")
                 if any(norm(s) in v for v in vs)]
            f += [f'masthead nav "{m.group(0)}" in the PDF text' for v in vs[:1] for m in nav_rx.finditer(v)]
            (R.fail("N4", f"{prel}: masthead/footer printed", f) if f else
             R.ok("N4", f"{prel}: no masthead nav ({' '.join(t for t, _ in exp_nav)}) and no footer text in the PDF"))

    # N5 sitemap
    if "sitemap.xml" in bundle.files:
        sm = bundle.files["sitemap.xml"].decode("utf-8", "replace")
        base = site["base_url"].rstrip("/")
        got = [html.unescape(x)[len(base):] or "/" if html.unescape(x).startswith(base) else html.unescape(x)
               for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", sm, re.S)]
        want = spec["sitemap"]
        (R.ok("N5", f"sitemap.xml lists exactly {want}") if sorted(got) == sorted(want) and len(got) == len(set(got))
         else R.fail("N5", f"sitemap.xml lists {got} (expected exactly {want})"))
    else:
        R.fail("N5", "sitemap.xml missing")


# ================================================================= check M

_MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
           r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_TOK_URL = re.compile(r"(?:https?://|www\.)\S+")
_TOK_HEX = re.compile(r"(?<![\w])[0-9a-f]{12,}(?![\w])")
_TOK_FILE = re.compile(r"[\w<>~./-]*\.(?:zip|xlsx|xls|csv|py|md|json|txt|log|pdf|html?|svg|png)\b", re.I)
_TOK_DATE = re.compile(r"(?<![\w.])\d{4}-\d{2}-\d{2}(?!\w)"
                       rf"|\b{_MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}}\b"
                       rf"|\b{_MONTHS}\.?\s+\d{{4}}\b"
                       r"|(?<![\w.])\d{1,2}/\d{1,2}/\d{2,4}(?!\w)"
                       r"|(?<![\w.,$+-])(?:19|20)\d{2}(?![\w%]|[.,]\d)")
_NUM_BODY = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_TOK_PAIR = re.compile(rf"(?<![\w.])[+-]{_NUM_BODY}\s*/\s*[+-]{_NUM_BODY}(?![\w.,]\d|\w)")
_TOK_NUM = re.compile(r"(?<![\w.])(?:[+-]?\$|[+-])?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:%|[kMBT](?![A-Za-z]))?")


def numeric_tokens(text: str) -> tuple[list[str], list[str]]:
    """(numbers, dates) in normalized text. Numbers: integers, decimals, comma-grouped,
    percentages, $ amounts, k/M/B/T suffixes, signed values and +N/-N pairs (one token).
    URLs, hex hashes and file names are dropped first; dates (ISO, 'Month D, YYYY',
    'Month YYYY', m/d/y, bare 19xx/20xx years) are returned separately."""
    t = _TOK_URL.sub(" ", text)
    t = _TOK_HEX.sub(" ", t)
    t = _TOK_FILE.sub(" ", t)
    dates: list[str] = []
    t = _TOK_DATE.sub(lambda m: dates.append(re.sub(r"\s+", " ", m.group(0))) or " ", t)
    toks: list[str] = []
    t = _TOK_PAIR.sub(lambda m: toks.append(re.sub(r"\s+", "", m.group(0))) or " ", t)
    toks += [m.group(0) for m in _TOK_NUM.finditer(t)]
    return toks, dates


def method_expected(spec: dict, src: Sources) -> dict:
    """What /method must carry from METHOD.md, derived from the file itself."""
    ms = spec["method"]
    text = src.text(ms["source_md"])
    lines = text.split("\n")
    run = [ln for ln in lines if ln.strip().startswith(ms["run_line_prefix"])]
    starts = [i for i, ln in enumerate(lines) if ln.strip() == ms["extract_from"]]
    errs = []
    if len(run) != 1:
        errs.append(f'{ms["source_md"]}: {len(run)} lines start with "{ms["run_line_prefix"]}" (expected 1)')
    if len(starts) != 1:
        errs.append(f'{ms["source_md"]}: {len(starts)} "{ms["extract_from"]}" lines (expected 1)')
    blocks = md_blocks("\n".join(lines[starts[0]:])) if len(starts) == 1 else []
    renames = {norm(k): norm(v) for k, v in ms.get("heading_renames", {}).items()}
    hits = {k: 0 for k in renames}
    fixed = []
    for kind, v in blocks:
        if kind.startswith("h") and v in renames:
            hits[v] += 1
            v = renames[v]
        fixed.append((kind, v))
    for k, n in hits.items():
        if n != 1:
            errs.append(f'{ms["source_md"]}: heading "{k}" found {n}x after "{ms["extract_from"]}" (expected 1)')
    words = block_words(fixed)
    drop = norm(ms["dropped_sentence"]).split()
    at = find_seq(words, drop)
    if len(at) != 1:
        errs.append(f'{ms["source_md"]}: the dropped sentence occurs {len(at)}x in the extracted part (expected 1)')
    else:
        words = words[:at[0]] + words[at[0] + len(drop):]
    return {"run": norm(md_inline(run[0])).split() if len(run) == 1 else [],
            "body": words, "blocks": fixed, "errs": errs,
            "headings": [v for k, v in fixed if k.startswith("h")],
            "tables": [v for k, v in fixed if k == "table"],
            "pres": [v for k, v in fixed if k == "pre"], "raw": text}


NUMERIC_CELL = re.compile(r"[\s$<>+~%.,\-0-9]*[0-9][\s$<>+~%.,\-0-9]*")


def right_aligned(cell: Node, num_classes: set[str]) -> bool:
    st = (cell.get("style") or "").replace(" ", "").lower()
    return bool(cell.classes() & num_classes) or "text-align:right" in st or (cell.get("align") or "").lower() == "right"


def check_method(bundle: Bundle, spec: dict, src: Sources, R: Report) -> None:
    ms = spec["method"]
    rel = ms["page"]
    R.section(f"M. {ms['url']} - METHOD PAGE")
    if rel not in bundle.files:
        for cid in ("M1", "M2", "M3"):
            R.fail(cid, f"{rel} missing from {bundle.origin}")
        return
    pg = bundle.page(rel)
    main = main_of(pg)
    blocks = visible_blocks(main)
    words = " ".join(blocks).split()
    exp = method_expected(spec, src)

    # ---- M1
    f, ok = list(exp["errs"]), []
    if page_title(pg) != [ms["title"]]:
        f.append(f"<title> {page_title(pg)} (expected {ms['title']!r})")
    if page_description(pg) != [ms["description"]]:
        f.append(f"meta description {page_description(pg)} (expected {ms['description']!r})")
    ipath = src.content / ms["intro_md"]
    links: list[tuple[str, str]] = []
    if not ipath.exists():
        f.append(f"{ipath} missing - cannot compare the intro")
        intro = []
    else:
        intro = md_blocks(ipath.read_text(encoding="utf-8"), links)
        pinned = md_blocks("\n".join(ms["intro_expected"]))
        if intro != pinned:
            d = next((i for i, (a, b) in enumerate(zip(intro, pinned)) if a != b), min(len(intro), len(pinned)))
            f.append(f"{ipath.name} differs from the copy pinned in checks/pages.json at block {d + 1}: "
                     f"file {ascii_ctx(str(intro[d:d + 1]), 120)} vs pinned {ascii_ctx(str(pinned[d:d + 1]), 120)}")
    itexts = [v for _, v in intro]
    at = [i for i in range(len(blocks) - len(itexts) + 1) if blocks[i:i + len(itexts)] == itexts] if itexts else []
    if itexts and not at:
        miss = next((t for t in itexts if t not in blocks), None)
        f.append("intro not rendered verbatim: " + (f'block "{ascii_ctx(miss, 90)}" missing; closest: "{closest(miss, blocks)}"'
                                                    if miss else "all blocks present but not contiguous/in order"))
    elif itexts:
        ok.append(f"intro: {len(itexts)} blocks of {ms['intro_md']} verbatim and contiguous")
    for t, h in links:
        if not any(a.tag == "a" and norm(a.text()) == t and a.get("href") == h for a in main.iter()):
            f.append(f'intro link "{t}" -> {h} missing')
    if links and not any("intro link" in x for x in f):
        ok.append(f"intro links: {', '.join(h for _, h in links)}")
    heads = [(n.tag, norm(n.text())) for n in main.iter() if re.fullmatch(r"h[1-6]", n.tag or "")]
    want = ([("h1", itexts[0])] if intro and intro[0][0] == "h1" else []) + [("h2", ms["h2"])] + [("h3", t) for t in ms["h3"]]
    if heads != want:
        f.append(f"heading outline {heads} != expected {want}")
    else:
        ok.append(f"outline: h1 {want[0][1]!r}, h2 {ms['h2']!r}, h3 {ms['h3']}")
    src_heads = [h for h in exp["headings"]]
    if src_heads != ms["h3"]:
        f.append(f"METHOD.md headings after the extraction point are {src_heads}, checks/pages.json expects {ms['h3']}")
    for s in ms["must_contain"]:
        (ok.append(f'contains "{ascii_ctx(s, 70)}"') if norm(s) in pg.text else f.append(f'missing "{s}"'))
    for s in ms["must_not_contain"]:
        ns = norm_ci(s)
        where = [m for m, vs in bundle.searchable(rel).items() if any(ns in v.casefold() for v in vs)]
        (f.append(f'"{s}" present ({", ".join(where)})') if where else ok.append(f'absent: "{s}"'))
    # the extraction, word for word
    h2pos = next((sum(len(b.split()) for b in blocks[:i]) for i, b in enumerate(blocks) if b == ms["h2"]), None)
    rpos = find_seq(words, exp["run"]) if exp["run"] else []
    bpos = find_seq(words, exp["body"]) if exp["body"] else []
    if exp["run"] and not rpos:
        f.append(f'"Run:" line not verbatim: {seq_break(words, exp["run"])}')
    if exp["body"] and not bpos:
        f.append(f"METHOD.md from \"{ms['extract_from']}\" to the end is not verbatim: {seq_break(words, exp['body'])}")
    if rpos and bpos and h2pos is not None:
        if not (h2pos < rpos[0] < bpos[0]):
            f.append(f"order: h2 at word {h2pos}, Run line at {rpos[0]}, extraction at {bpos[0]} (expected h2 < Run < extraction)")
        else:
            ok.append(f"Run line + {len(exp['body']):,} words of METHOD.md (\"{ms['extract_from']}\" to the end, "
                      f"less the dropped sentence) verbatim after the h2")
    # real tables, numeric columns right-aligned
    tables = [t for t in main.iter() if t.tag == "table"]
    num_classes = set()
    for origin, css in page_css(bundle, pg):
        for sel, body, ctx in css_rules(css):
            if css_decls(body).get("text-align", "").startswith("right") and not any("print" in c.lower() for c in ctx):
                num_classes |= {k[1:] for k in selector_keys(sel) if k.startswith(".")}
    if len(tables) != len(exp["tables"]):
        f.append(f"{len(tables)} <table> elements, METHOD.md's extracted part has {len(exp['tables'])} tables")
    for k, (t, mdt) in enumerate(zip(tables, exp["tables"]), 1):
        grid = table_grid(t)
        got = [[cell_text(c) for c in cells] for _, cells, _ in grid]
        if got != mdt:
            d = next((i for i, (a, b) in enumerate(zip(got, mdt)) if a != b), min(len(got), len(mdt)))
            f.append(f"table {k}: row {d + 1} is {got[d:d + 1]} but METHOD.md has {mdt[d:d + 1]}")
            continue
        body = [cells for _, cells, head in grid if not head]
        for c in range(1, len(grid[0][1]) if grid else 0):
            vals = [cell_text(r[c]) for r in body if c < len(r)]
            share = sum(bool(NUMERIC_CELL.fullmatch(v)) for v in vals) / len(vals) if vals else 0
            cells = [grid[0][1][c]] + [r[c] for r in body if c < len(r)]
            aligned = all(right_aligned(x, num_classes) for x in cells)
            if share == 1 and not aligned:
                f.append(f'table {k} ("{mdt[0][0]}"): numeric column "{mdt[0][c]}" is not right-aligned')
            elif share >= 0.6 and not aligned:
                R.warn("M1", f'table {k} ("{mdt[0][0]}"): mostly numeric column "{mdt[0][c]}" is not right-aligned')
    if tables and not any(x.startswith("table") for x in f):
        ok.append(f"{len(tables)} real tables == METHOD.md cell for cell; numeric columns right-aligned "
                  f"(classes {sorted(num_classes)})")
    pres = [norm(n.text()) for n in main.iter() if n.tag == "pre"]
    for p in exp["pres"]:
        (ok.append(f"code block verbatim ({len(p.split())} words)") if p in pres
         else f.append(f'code block missing or altered: "{ascii_ctx(p, 80)}"'))
    (R.fail("M1", f"{rel}: structure and verbatim copy", f + ok) if f else
     R.ok("M1", f"{rel}: structure and verbatim copy", ok))

    # ---- M2 numeric provenance
    srcs, f = [], []
    for s in spec["method"]["provenance_sources"]:
        t = norm(src.text(s))
        if s == ms["source_md"]:
            d = norm(ms["dropped_sentence"])
            if t.count(d) != 1:
                f.append(f"{s}: the dropped sentence occurs {t.count(d)}x (expected 1)")
            t = t.replace(d, " ")
        srcs.append(t)
    src_toks, src_dates = numeric_tokens(" ".join(srcs))
    src_set, date_set = set(src_toks), set(src_dates)
    ptext = " | ".join([pg.text] + meta_texts(pg))
    toks, dates = numeric_tokens(ptext)
    orphans = []
    for tk in dict.fromkeys(toks):
        if tk not in src_set:
            m = re.search(r"(?<![\w.])" + re.escape(tk).replace("/", r"\s*/\s*") + r"(?![\w])", ptext)
            k = m.start() if m else max(ptext.find(tk), 0)
            orphans.append(f'"{tk}" in "{ctx_around(ptext, k, k + len(tk), 45)}"')
    f += [f"orphan number (not in {' or '.join(ms['provenance_sources'])}): {o}" for o in orphans]
    odates = sorted(set(dates) - date_set)
    if odates:
        R.warn("M2", f"{rel}: dates not found in the sources (dates are outside M2's number check): {odates}")
    (R.fail("M2", f"{rel}: numeric provenance ({len(toks)} numeric tokens)", f) if f else
     R.ok("M2", f"{rel}: all {len(toks)} numeric tokens ({len(set(toks))} distinct) occur in "
                f"{' or '.join(ms['provenance_sources'])} (dropped sentence excluded); {len(dates)} dates set aside",
          [", ".join(list(dict.fromkeys(toks))[:40]) + (" ..." if len(set(toks)) > 40 else "")]))

    # ---- M3 churn figures vs report_stats.log section 8
    ch = ms["churn"]
    log = src.stats_log
    if not log.exists():
        R.skip_optional("M3", f"{log} not found - churn figures not cross-checked against the log")
        return
    lt = log.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*" + re.escape(ch["stats_log_section"]) + r"\s.*$", lt, re.M)
    sec = lt[m.end():] if m else ""
    nxt = re.search(r"^\s*\d+\.\s+[A-Z]", sec, re.M)
    sec = sec[:nxt.start()] if nxt else sec
    trans = [(a, b, f"{p}/{q}") for a, b, p, q in
             re.findall(r"^\s*(\S+)\s+->\s+(\S+)\s+([+-]\d[\d,]*)\s*/\s*([+-]\d[\d,]*)\s*$", sec, re.M)]
    if not trans:
        R.skip_optional("M3", f"{log.name} has no section {ch['stats_log_section']} transition lines "
                              f"(archive/raw missing when it ran?) - churn figures not cross-checked")
        return
    page_pairs = [re.sub(r"\s+", "", x) for x in _TOK_PAIR.findall(pg.text)]
    log_pairs = [p for _, _, p in trans]
    f, ok = [], [f"log section {ch['stats_log_section']}: " + "; ".join(f"{a} -> {b} {p}" for a, b, p in trans)]
    if set(page_pairs) != set(log_pairs):
        f.append(f"pairs quoted on the page {page_pairs} != the log's {log_pairs}")
    if set(log_pairs) != set(ch["pairs"]):
        f.append(f"the log's pairs {log_pairs} != checks/pages.json {ch['pairs']} (log regenerated? review the page)")
    zero = [(a, b) for a, b, p in trans if p == "+0/-0"]
    if zero != [tuple(ch["zero_transition"])]:
        f.append(f"+0/-0 transitions in the log: {zero} (expected {ch['zero_transition']})")
    if norm(ch["zero_sentence"]) not in pg.text:
        f.append(f'page lacks "{ch["zero_sentence"]}"')
    nz_page = [p for p in dict.fromkeys(page_pairs) if p != "+0/-0"]
    nz_log = [p for p in log_pairs if p != "+0/-0"]
    if nz_page != nz_log:
        f.append(f"adjacent-month pairs on the page in order {nz_page}, the log has {nz_log}")
    (R.fail("M3", f"{rel}: month-to-month churn vs {log.name}", f + ok) if f else
     R.ok("M3", f"{rel}: churn {', '.join(nz_page)} and +0/-0 ({' -> '.join(ch['zero_transition'])}) "
                f"match {log.name} section {ch['stats_log_section']}", ok))


# ================================================================= check D

def read_manifest(path: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"))))


def stale_files(rows: list[dict]) -> set[str]:
    """Contract rule: stale if data_through <= max data_through of earlier vintages, same variant."""
    out = set()
    for r in rows:
        earlier = [e["data_through"] for e in rows if e["variant_by_content"] == r["variant_by_content"]
                   and e["vintage"] < r["vintage"] and e.get("data_through")]
        if earlier and r.get("data_through") and r["data_through"] <= max(earlier):
            out.add(r["filename"])
    return out


def stale_files_prev(rows: list[dict]) -> set[str]:
    """fetch_adv.py stale_against, literally: compared with the previous vintage only."""
    out = set()
    for r in rows:
        earlier = [e for e in rows if e["variant_by_content"] == r["variant_by_content"]
                   and e["vintage"] < r["vintage"] and e.get("data_through")]
        if earlier and r.get("data_through") and r["data_through"] <= max(earlier, key=lambda e: e["vintage"])["data_through"]:
            out.add(r["filename"])
    return out


def parse_data_tables(pg: Page, ds: dict):
    """{variant: [row dict]} and errors, from the /data page's per-section tables."""
    cols, dag = ds["columns"], ds["dagger"]
    out, errs = {}, []
    for heading, variant in ds["sections"]:
        hs = [n for n in pg.root.iter() if n.tag == "h2" and norm(n.text()) == heading]
        if len(hs) != 1:
            errs.append(f'{len(hs)} <h2>{heading}</h2> (expected 1)')
            continue
        t = first_after(pg, hs[0], ("table",))
        if t is None:
            errs.append(f'no <table> under <h2>{heading}</h2>')
            continue
        grid = table_grid(t)
        header = [cell_text(c) for c in grid[0][1]] if grid and grid[0][2] else []
        if header != cols:
            errs.append(f'"{heading}" header {header} != {cols}')
            continue
        ix = {c: i for i, c in enumerate(cols)}
        rows = []
        for tr, cells, head in grid[1:]:
            if head or len(cells) != len(cols):
                errs.append(f'"{heading}": row with {len(cells)} cells: {[cell_text(c) for c in cells]}')
                continue
            fc, sc = cells[ix["File"]], cells[ix["sha256"]]
            a = [n for n in fc.iter() if n.tag == "a"]
            codes = [n for n in sc.iter() if n.tag == "code"]
            through = cell_text(cells[ix["Data through"]])
            rows.append({"file": cell_text(fc), "href": a[0].get("href") if a else None, "n_links": len(a),
                         "strong": [norm(n.text()) for n in fc.iter() if n.tag in ("strong", "b")],
                         "classes": tr.classes(), "date": cell_text(cells[ix["File date"]]),
                         "through_raw": through, "dagger": dag in through, "through": through.replace(dag, "").strip(),
                         "rows": cell_text(cells[ix["Rows"]]), "source": cell_text(cells[ix["Source"]]),
                         "sha_text": cell_text(sc), "sha_code": [norm(n.text()) for n in codes],
                         "sha_titles": [n.get("title") for n in sc.iter() if n.get("title")]})
        out[variant] = rows
    return out, errs


def adv_sources_table(pg: Page, sid: str):
    """{filename: {...}} from the /adv Sources table (the section with id=sid)."""
    hs = [n for n in pg.root.iter() if n.tag == "h2" and n.get("id") == sid]
    if len(hs) != 1:
        return None, f'{len(hs)} <h2 id="{sid}"> on {pg.rel}'
    t = first_after(pg, hs[0], ("table",))
    if t is None:
        return None, f"no table in the {sid} section of {pg.rel}"
    grid = table_grid(t)
    head = [cell_text(c) for c in grid[0][1]] if grid and grid[0][2] else []
    need = ("File", "sha256", "Rows", "Data through")
    if not all(k in head for k in need):
        return None, f"{pg.rel} sources table header {head} lacks one of {need}"
    ix = {k: head.index(k) for k in need}
    out = {}
    for tr, cells, is_head in grid[1:]:
        if is_head or len(cells) != len(head):
            continue
        a = [n for n in cells[ix["File"]].iter() if n.tag == "a"]
        out[cell_text(cells[ix["File"]])] = {"sha256": cell_text(cells[ix["sha256"]]), "rows": cell_text(cells[ix["Rows"]]),
                                             "through": cell_text(cells[ix["Data through"]]),
                                             "href": a[0].get("href") if a else None}
    return out, None


def method_md_sources(text: str, heading: str):
    lines = text.split("\n")
    st = [i for i, ln in enumerate(lines) if ln.strip() == heading]
    if len(st) != 1:
        return None, f'{len(st)} "{heading}" lines in METHOD.md'
    rows = []
    for ln in lines[st[0] + 1:]:
        if ln.lstrip().startswith("#"):
            break
        if ln.strip().startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                rows.append(cells)
    if not rows:
        return None, "no table under METHOD.md ## Sources"
    head = rows[0]
    ix = {k: head.index(k) for k in ("File", "sha256", "Rows", "Data through") if k in head}
    if len(ix) != 4:
        return None, f"METHOD.md sources header {head}"
    out = {}
    for r in rows[1:]:
        fm = re.search(r"`([^`]+)`", r[ix["File"]])
        um = re.search(r"\]\(([^)\s]+)\)", r[ix["File"]])
        sm = re.search(r"([0-9a-f]{64})", r[ix["sha256"]])
        if fm:
            out[fm.group(1)] = {"sha256": sm.group(1) if sm else r[ix["sha256"]], "rows": r[ix["Rows"]].strip(),
                                "through": r[ix["Data through"]].strip(), "href": um.group(1) if um else None}
    return out, None


def check_data(bundle: Bundle, spec: dict, src: Sources, R: Report) -> None:
    ds = spec["data"]
    rel = ds["page"]
    R.section(f"D. {ds['url']} - DATA PAGE vs archive/MANIFEST.csv")
    mpath = src.repo / ds["manifest"]
    if not mpath.exists():
        for cid in ("D1", "D2", "D3", "D4"):
            R.fail(cid, f"{mpath} not found - nothing to compare /data against")
        return
    man = read_manifest(mpath)
    by_var: dict[str, list[dict]] = {}
    for r in man:
        by_var.setdefault(r["variant_by_content"], []).append(r)
    known = {v for _, v in ds["sections"]}
    stale = stale_files(man)
    if rel not in bundle.files:
        for cid in ("D1", "D2", "D3"):
            R.fail(cid, f"{rel} missing from {bundle.origin}")
        tables = None
    else:
        pg = bundle.page(rel)
        tables, errs = parse_data_tables(pg, ds)

    # ---- D1
    if tables is not None:
        f, ok = list(errs), []
        if page_title(pg) != [ds["title"]]:
            f.append(f"<title> {page_title(pg)} (expected {ds['title']!r})")
        if page_description(pg) != [ds["description"]]:
            f.append(f"meta description {page_description(pg)} (expected {ds['description']!r})")
        other = sorted(set(by_var) - known)
        if other:
            f.append(f"MANIFEST variant_by_content values with no section: {other} "
                     f"({sum(len(by_var[v]) for v in other)} rows)")
        ipath = src.content / ds["intro_md"]
        links: list[tuple[str, str]] = []
        main = main_of(pg)
        blocks = visible_blocks(main)
        if not ipath.exists():
            f.append(f"{ipath} missing - cannot compare the intro")
        else:
            intro = md_blocks(ipath.read_text(encoding="utf-8"), links)
            if intro != md_blocks("\n".join(ds["intro_expected"])):
                f.append(f"{ipath.name} differs from the copy pinned in checks/pages.json")
            it = [v for _, v in intro]
            if not any(blocks[i:i + len(it)] == it for i in range(len(blocks) - len(it) + 1)):
                miss = next((t for t in it if t not in blocks), None)
                f.append("intro not rendered verbatim: " + (f'"{ascii_ctx(miss, 90)}" missing; closest: "{closest(miss, blocks)}"'
                                                            if miss else "blocks not contiguous/in order"))
            else:
                ok.append(f"intro: {len(it)} blocks of {ds['intro_md']} verbatim")
            for t, h in links:
                if not any(a.tag == "a" and norm(a.text()) == t and a.get("href") == h for a in main.iter()):
                    f.append(f'intro link "{t}" -> {h} missing')
        labels = ds["source_labels"]
        for heading, variant in ds["sections"]:
            if variant not in tables:
                continue
            got = {r["file"]: r for r in tables[variant]}
            want = by_var.get(variant, [])
            g = []
            if len(tables[variant]) != len(want):
                g.append(f"{len(tables[variant])} rows, MANIFEST has {len(want)} {variant} files")
            dup = [k for k, c in Counter(r["file"] for r in tables[variant]).items() if c > 1]
            extra = sorted(set(got) - {r["filename"] for r in want})
            if dup:
                g.append(f"duplicate rows: {dup}")
            if extra:
                g.append(f"rows not in MANIFEST: {extra}")
            dates = [r["date"] for r in tables[variant]]
            if dates != sorted(dates, reverse=True):
                g.append("rows are not sorted by file date, newest first")
            for m in want:
                fn, sha = m["filename"], m["sha256"]
                r = got.get(fn)
                if r is None:
                    g.append(f"{fn} missing")
                    continue
                exp_src = labels.get(m["source"])
                bad = []
                if r["href"] != m["url"] or r["n_links"] != 1:
                    bad.append(f"link {r['href']!r} (x{r['n_links']}) != {m['url']}")
                if r["date"] != m["vintage"]:
                    bad.append(f"file date {r['date']!r} != {m['vintage']}")
                if r["through"] != m["data_through"] or r["through_raw"] not in (m["data_through"], f"{m['data_through']} {ds['dagger']}"):
                    bad.append(f"data through {r['through_raw']!r} != {m['data_through']}")
                if r["rows"] != f"{int(m['n_rows']):,}":
                    bad.append(f"rows {r['rows']!r} != {int(m['n_rows']):,}")
                if exp_src is None:
                    bad.append(f"MANIFEST source {m['source']!r} has no label")
                elif r["source"] != exp_src:
                    bad.append(f"source {r['source']!r} != {exp_src!r} (MANIFEST {m['source']})")
                p = sha[:ds["sha_prefix_len"]]
                if r["sha_code"] != [p] or r["sha_text"] != p:
                    bad.append(f"sha256 cell {r['sha_text']!r} (<code> {r['sha_code']}) != {p}")
                if sha not in r["sha_titles"]:
                    bad.append(f"full sha256 not in a title attribute (titles {[ascii_ctx(t, 20) for t in r['sha_titles']]})")
                if bad:
                    g.append(f"{fn}: " + "; ".join(bad))
            f += [f'"{heading}": {x}' for x in g]
            if not g:
                ok.append(f'"{heading}": {len(want)} rows == MANIFEST {variant} (file+link, file date, data through, '
                          f'rows, source, sha256[:{ds["sha_prefix_len"]}] + full hash in title), newest first')
        (R.fail("D1", f"{rel}: tables vs {ds['manifest']}", f + ok) if f else
         R.ok("D1", f"{rel}: tables vs {ds['manifest']} ({len(man)} files)", ok))

    # ---- D2 panel files across /data, /adv Sources, METHOD.md Sources
    if tables is not None:
        f, ok = [], []
        pf = ds["panel_files"]
        rows = [r for v in tables.values() for r in v]
        marked = sorted(r["file"] for r in rows if ds["panel_class"] in r["classes"])
        bold = sorted(r["file"] for r in rows if r["strong"])
        if marked != sorted(pf):
            f.append(f'rows with class="{ds["panel_class"]}": {marked} (expected {sorted(pf)})')
        if bold != sorted(pf):
            f.append(f"rows with <strong>: {bold} (expected {sorted(pf)})")
        adv, e1 = (adv_sources_table(bundle.page(ds["adv_page"]), ds["adv_sources_id"])
                   if ds["adv_page"] in bundle.files else (None, f"{ds['adv_page']} missing"))
        mdsrc, e2 = method_md_sources(src.text(spec["method"]["source_md"]), ds["method_sources_heading"])
        f += [e for e in (e1, e2) if e]
        mrow = {r["filename"]: r for r in man}
        for fn, want in pf.items():
            d = next((r for r in rows if r["file"] == fn), None)
            views = {"pinned": {"rows": want["rows"], "sha256": want["sha256"], "through": want["data_through"]}}
            if d:
                if fn not in d["strong"]:
                    f.append(f"{fn}: file name not wrapped in <strong> on /data")
                full = next((t for t in d["sha_titles"] if re.fullmatch(r"[0-9a-f]{64}", t or "")), None)
                views["/data"] = {"rows": d["rows"], "sha256": full, "through": d["through"], "href": d["href"]}
            else:
                f.append(f"{fn}: no row on /data")
            if adv is not None:
                a = adv.get(fn)
                (f.append(f"{fn}: not in the /adv Sources table") if a is None else views.__setitem__("/adv", a))
            if mdsrc is not None:
                mm = mdsrc.get(fn)
                (f.append(f"{fn}: not in METHOD.md Sources") if mm is None else views.__setitem__("METHOD.md", mm))
            if fn in mrow:
                views["MANIFEST"] = {"rows": f"{int(mrow[fn]['n_rows']):,}", "sha256": mrow[fn]["sha256"],
                                     "through": mrow[fn]["data_through"], "href": mrow[fn]["url"]}
            for key in ("rows", "sha256", "through", "href"):
                vals = {k: v.get(key) for k, v in views.items() if key in v}
                if len(set(vals.values())) > 1:
                    f.append(f"{fn} {key} disagrees: " + "; ".join(f"{k}={ascii_ctx(str(v), 70)}" for k, v in vals.items()))
            if not any(x.startswith(fn) for x in f):
                ok.append(f"{fn}: rows {want['rows']}, sha256 {want['sha256'][:12]}..., data through "
                          f"{want['data_through']} agree across {', '.join(views)}")
        (R.fail("D2", f"{rel}: panel files, cross-page", f + ok) if f else
         R.ok("D2", f"{rel}: panel files bold + class {ds['panel_class']}; /data == /adv Sources == METHOD.md == MANIFEST", ok))

    # ---- D3 stale daggers
    prev = stale_files_prev(man)
    exp_stale = set(ds["expected_stale"])
    f = []
    if stale != prev:
        R.warn("D3", f"the contract's max-rule stale set {sorted(stale)} differs from fetch_adv.py's "
                     f"previous-vintage rule {sorted(prev)}")
    if stale != exp_stale:
        f.append(f"MANIFEST stale set is {sorted(stale)}; checks/pages.json expects {sorted(exp_stale)} "
                 f"(the manifest changed - review /data and update the expectation)")
    if "ia09022025.xlsx" not in stale:
        f.append("ia09022025.xlsx is not stale by the rule (it is the July 2025 roster under a September name)")
    if tables is not None:
        dagged = {r["file"] for v in tables.values() for r in v if r["dagger"]}
        if dagged != stale:
            f.append(f"daggers on {sorted(dagged)}; the stale rule gives {sorted(stale)} "
                     f"(missing {sorted(stale - dagged)}, extra {sorted(dagged - stale)})")
        main = main_of(pg)
        order = {id(n): i for i, n in enumerate(pg.root.iter())}
        last_table = max((order[id(t)] for t in main.iter() if t.tag == "table"), default=-1)
        note_nodes = [n for n in main.iter() if n.tag not in INLINE and n.tag != "main"
                      and norm(" ".join(visible_blocks(n))) == norm(ds["note"])]
        if not note_nodes:
            f.append(f'note "{ds["note"]}" missing')
        elif order[id(note_nodes[0])] < last_table:
            f.append("the dagger note is not under the tables")
    (R.fail("D3", f"{rel}: stale markers", f) if f else
     R.ok("D3", f"{rel}: daggers exactly on {sorted(stale)} (= stale rule on MANIFEST = checks/pages.json); "
                f"note under the tables"))

    # ---- D4 manifest copy
    cp = ds["manifest_copy"]
    if cp not in bundle.files:
        R.fail("D4", f"{cp} missing from {bundle.origin}")
    else:
        a, b = bundle.files[cp], mpath.read_bytes()
        ha, hb = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
        (R.ok("D4", f"{cp} == {ds['manifest']} byte for byte ({len(a):,} B, sha256 {ha[:16]}...)") if a == b else
         R.fail("D4", f"{cp} ({len(a):,} B, sha256 {ha[:16]}...) != {ds['manifest']} ({len(b):,} B, sha256 {hb[:16]}...)"))


# ================================================================= check K2 / H2

def _compound_matches(comp: str, el: Node) -> bool:
    comp = re.sub(r"::?[\w-]+(\([^)]*\))?|\[[^\]]*\]", "", comp)
    tag = re.match(r"^([a-zA-Z][\w-]*)", comp)
    if tag and tag.group(1).lower() != el.tag:
        return False
    if not set(re.findall(r"\.([\w-]+)", comp)) <= el.classes():
        return False
    ids = re.findall(r"#([\w-]+)", comp)
    return not ids or ids == [el.get("id")]


def _specificity(sel: str) -> tuple[int, int, int]:
    """CSS specificity (ids, classes/attributes/pseudo-classes, types) of one selector."""
    s = re.sub(r"::[\w-]+(\([^)]*\))?", " x ", sel)
    ids = len(re.findall(r"#[\w-]+", s))
    cls = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:[\w-]+(\([^)]*\))?", s))
    types = len(re.findall(r"(?:^|[\s>+~(])([a-zA-Z][\w-]*)", s))
    return ids, cls, types


HALO_PROPS = ("paint-order", "stroke", "stroke-width", "stroke-linejoin")


def check_halo(bundle: Bundle, works: list[dict], spec: dict, R: Report) -> None:
    hs = spec.get("halo")
    if not hs:
        return
    R.section("K2. CHART-LABEL HALO")

    def breaks(prop: str, val: str) -> bool:
        v = val.replace("!important", "").strip()
        if prop == "paint-order":
            return (v.split() or [""])[0] != hs["paint_order_first"]
        if prop == "stroke":
            return "var(" + hs["stroke_var"] not in v.replace(" ", "")
        if prop == "stroke-width":
            mw = re.fullmatch(r"([\d.]+)(px)?", v)
            return not mw or not 0 < float(mw.group(1)) <= hs["max_stroke_width_px"]
        return v != hs["linejoin"]

    for w in works:
        rel = w["page"]
        if rel not in bundle.files:
            continue
        pg = bundle.page(rel)
        charts = [s for s in svg_top(pg.root) if any(n.tag == "text" for n in s.iter())]
        if not charts:
            continue

        def reaches(one: str) -> bool:
            parts = [p for p in re.split(r"\s*[>+~]\s*|\s+", one.strip()) if p]
            if not parts or not re.match(r"text(?![\w-])", parts[-1]) or \
                    not _compound_matches(parts[-1], Node("text")):
                return False  # the rule's subject must be the <text> element itself
            return all(all(any(_compound_matches(p, el) for el in [s] + list(s.ancestors()) if el.tag)
                           for p in parts[:-1]) for s in charts)

        merged, hits, bg = {}, [], {"light": False, "dark": False, "print": False}
        base_rank: dict[str, tuple] = {}      # prop -> (important, specificity, order) of the unwrapped winner
        overrides = []                        # (context, selector, prop, value, rank) inside @media
        order = 0
        for origin, css in page_css(bundle, pg):
            for sel, body, ctx in css_rules(css):
                if sel.startswith("@"):
                    continue
                order += 1
                d = css_decls(body)
                c = " ".join(ctx).lower().replace(" ", "")
                if "--bg" in d:
                    bg["print" if "print" in c else "dark" if is_dark_ctx(sel, ctx) else "light"] = True
                wrapped = any(not re.match(r"@(supports|layer)\b", p, re.I) for p in ctx)
                for one in sel.split(","):
                    if not reaches(one):
                        continue
                    props = {k: v for k, v in d.items() if k in HALO_PROPS}
                    if wrapped:
                        # The halo itself must not live in an @media wrapper, but a wrapped rule
                        # can still switch it off in one medium or theme (print, dark, screen).
                        for k, v in props.items():
                            overrides.append((" ".join(ctx), one.strip(), k, v,
                                              ("!important" in v, _specificity(one), order)))
                        continue
                    merged.update(d)
                    hits.append(one.strip())
                    for k, v in props.items():
                        rank = ("!important" in v, _specificity(one), order)
                        if k not in base_rank or rank >= base_rank[k]:
                            base_rank[k] = rank
        f, print_off = [], []
        pw = hs.get("print_without_halo")
        for ctx_s, one, k, v, rank in overrides:
            if not (breaks(k, v) and (k not in base_rank or rank >= base_rank[k])):
                continue
            # Print may drop the halo, but only on terms: Chrome's PDF engine emits
            # stroked text as Type3 glyph outlines, one font per size (the PDF went
            # from 245 KB to 1.6 MB) that also carry the text a second time, so
            # every chart label reads twice in the PDF's text layer. Without the
            # halo, every label that sits on a filled shape in the printed PDF must
            # still clear the contrast floor in checks/pages.json (measured below).
            # Screen, in both themes, always needs the halo.
            if pw and re.search(r"\bprint\b", ctx_s, re.I) and not re.search(r"\bscreen\b", ctx_s, re.I):
                print_off.append(f"{ctx_s}: {one} {{{k}: {v}}}")
                continue
            f.append(f"halo switched off inside {ctx_s}: {one} {{{k}: {v}}} wins over the unwrapped rule; "
                     + ("the labels need the halo on screen, in light and dark" if pw else
                        "the labels need the halo on screen and in print"))
        print_note = ""
        if print_off:
            pdfs = sorted(r for r in bundle.files if fnmatch.fnmatch(r, w.get("pdf_glob", "")) and r.lower().endswith(".pdf"))
            if not pdfs:
                f.append(f"print drops the halo ({print_off[0]}) but there is no PDF ({w.get('pdf_glob')}) to measure "
                         f"the printed labels in")
            for prel in pdfs:
                labs, err = pdf_labels_on_fills(bundle.files[prel], float(pw["min_overlap"]))
                if err:
                    f.append(f"{prel}: cannot measure printed labels: {err}")
                    continue
                low = [x for x in labs if x[4] < float(pw["min_contrast"])]
                f += [f'{prel} p{p}: "{ascii_ctx(t, 40)}" #{tc} on #{fc} is {r:.2f}:1 without the halo '
                      f'(print drops it: {print_off[0]}); needs >= {pw["min_contrast"]}:1' for p, t, tc, fc, r, _ in low]
                print_note += (f"; print drops it ({len(print_off)} rule(s)), and in {prel} "
                               + (", ".join(f'"{ascii_ctx(t, 30)}" #{tc} on #{fc} {r:.2f}:1' for p, t, tc, fc, r, _ in labs)
                                  or "no label sits on a filled shape")
                               + f" (floor {pw['min_contrast']}:1, overlap >= {float(pw['min_overlap']):.0%} of the label)")
        po = merged.get("paint-order", "").split()
        if not hits:
            f.append("no stylesheet rule reaches the <text> of every inline chart")
        if not po or po[0] != hs["paint_order_first"]:
            f.append(f"paint-order {merged.get('paint-order')!r} (expected '{hs['paint_order_first']} ...')")
        if "var(" + hs["stroke_var"] not in merged.get("stroke", "").replace(" ", ""):
            f.append(f"stroke {merged.get('stroke')!r} (expected var({hs['stroke_var']}))")
        mw = re.fullmatch(r"([\d.]+)(px)?", merged.get("stroke-width", "").replace("!important", "").strip())
        if not mw or not 0 < float(mw.group(1)) <= hs["max_stroke_width_px"]:
            f.append(f"stroke-width {merged.get('stroke-width')!r} (expected 0 < w <= {hs['max_stroke_width_px']}px)")
        if merged.get("stroke-linejoin", "").strip() != hs["linejoin"]:
            f.append(f"stroke-linejoin {merged.get('stroke-linejoin')!r} (expected {hs['linejoin']})")
        missing_bg = [k for k, v in bg.items() if not v]
        if missing_bg:
            f.append(f"{hs['stroke_var']} not defined for: {missing_bg}")
        inline = sum(1 for s in charts for n in s.iter() if n.tag == "text" and "stroke" in (n.get("style") or "")
                     .replace("stroke-linejoin", "").replace("stroke-linecap", ""))
        if inline:
            R.warn("K2", f"{rel}: {inline} chart <text> elements set stroke inline (overrides the halo)")
        n_text = sum(1 for s in charts for n in s.iter() if n.tag == "text")
        desc = (f"{rel}: {sorted(set(hits))} {{paint-order:{merged.get('paint-order')}; stroke:{merged.get('stroke')}; "
                f"stroke-width:{merged.get('stroke-width')}; stroke-linejoin:{merged.get('stroke-linejoin')}}} "
                f"reaches {n_text} <text> in {len(charts)} charts; {hs['stroke_var']} defined light/dark/print"
                + (print_note or "; the halo applies in print too"))
        (R.fail("K2", f"{rel}: chart-label halo", f) if f else R.ok("K2", desc))


def _srgb_lum(rgb) -> float:
    def ch(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def pdf_labels_on_fills(data: bytes, min_overlap: float):
    """Text runs in a PDF that sit on a filled, non-white shape (at least `min_overlap`
    of the run's box over it), with their contrast against it: [(page, text, text hex,
    fill hex, ratio, overlap)], error. Link underlines (a few % of a run) and page
    white do not count; gray and RGB fills are read, other colour spaces are an error."""
    try:
        fitz = _fitz()
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa
        return [], f"{type(e).__name__}: {e}"
    out = []
    try:
        for pno, page in enumerate(doc, 1):
            fills = []
            for d in page.get_drawings():
                c = d.get("fill")
                if c is None or (d.get("fill_opacity") or 1) < 0.5:
                    continue
                c = tuple(c)
                if len(c) == 1:
                    c = c * 3
                if len(c) != 3:
                    return out, f"page {pno}: a fill in a {len(c)}-component colour space"
                if min(c) > 0.95:
                    continue
                fills.append((fitz.Rect(d["rect"]), c))
            if not fills:
                continue
            for b in page.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    for s in ln["spans"]:
                        r = fitz.Rect(s["bbox"])
                        if not s["text"].strip() or r.is_empty:
                            continue
                        col = s["color"]
                        tc = (((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255)
                        for fr, fc in fills:
                            share = (r & fr).get_area() / r.get_area() if not (r & fr).is_empty else 0
                            if share < min_overlap:
                                continue
                            la, lb = _srgb_lum(tc), _srgb_lum(fc)
                            ratio = (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
                            out.append((pno, s["text"].strip(), f"{col:06x}",
                                        "".join(f"{round(v * 255):02x}" for v in fc), ratio, share))
    finally:
        doc.close()
    return out, None


def git_tracked(repo: Path) -> set[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True,
                             timeout=60).stdout
    except Exception:  # noqa
        return None
    return {p for p in out.decode("utf-8", "replace").split("\0") if p}


def check_repo_links(bundle: Bundle, site: dict, src: Sources, R: Report, works: list[dict] = ()) -> None:
    R.section("H2. LINKS INTO THE REPOSITORY")
    repo, branch = canon_url(site.get("repo_url")), site.get("repo_branch", "master")
    if not repo:
        R.fail("H2", "checks/site.json has no repo_url to check links against")
        return
    tracked = git_tracked(src.repo)
    # code spans that name a committed file (relative to the repo root or a work's source dir)
    # must link to that file, as the report page does
    dirs = [""] + sorted({str(Path(w["source_of_truth"]).parent.as_posix()) + "/" for w in works
                          if w.get("source_of_truth")})
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        f, linked = [], []
        for c in pg.root.iter():
            if c.tag != "code" or any(a.tag == "pre" for a in c.ancestors()):
                continue
            t = norm(c.text())
            if tracked is None or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]+", t) or ".." in t:
                continue
            hits = [d + t for d in dirs if d + t in tracked]
            if not hits:
                continue
            a = next((x for x in c.ancestors() if x.tag == "a"), None)
            want = {f"{repo}/blob/{branch}/{h}" for h in hits}
            if a is None or canon_url(a.get("href")) not in want:
                f.append(f"<code>{t}</code> names {hits[0]} but links {a.get('href') if a is not None else 'nowhere'} "
                         f"(expected {sorted(want)[0]})")
            else:
                linked.append(t)
        if f:
            R.fail("H2", f"{rel}: code spans naming committed repository files are not linked to them", f)
        elif linked:
            R.ok("H2", f"{rel}: code spans naming committed files link to them: {sorted(set(linked))}")
    for rel in bundle.html_rels():
        pg = bundle.page(rel)
        hrefs = [a.get("href").strip() for a in pg.nodes("a") if (a.get("href") or "").strip().lower().startswith(repo.lower())]
        if not hrefs:
            continue
        f, warn, kinds = [], [], Counter()
        for h in hrefs:
            rest = h[len(repo):]
            if rest in ("", "/"):
                kinds["repo root"] += 1
                continue
            m = re.fullmatch(r"/(blob|tree)/([^/]+)/(.+?)/?", rest.split("#")[0].split("?")[0])
            if not m:
                f.append(f"{h}: not <repo>, <repo>/blob/<branch>/<path> or <repo>/tree/<branch>/<path>")
                continue
            kind, br, path = m.group(1), m.group(2), urllib.parse.unquote(m.group(3))
            if br != branch:
                f.append(f"{h}: branch {br!r} (default branch is {branch!r})")
            p = src.repo / path
            if kind == "blob" and not p.is_file():
                f.append(f"{h}: {path} is not a file in the repository")
            elif kind == "tree" and not p.is_dir():
                f.append(f"{h}: {path} is not a directory in the repository")
            elif tracked is not None and not (path in tracked if kind == "blob"
                                              else any(t.startswith(path.rstrip("/") + "/") for t in tracked)):
                warn.append(f"{path} exists but is not committed yet (GitHub 404s until it is pushed)")
            kinds[f"{kind}/{br}"] += 1
        if warn:
            R.warn("H2", f"{rel}: repository links to uncommitted paths", sorted(set(warn)))
        (R.fail("H2", f"{rel}: repository links", f) if f else
         R.ok("H2", f"{rel}: {len(hrefs)} repository links resolve ({', '.join(f'{k} x{v}' for k, v in sorted(kinds.items()))})"
                    + ("" if tracked is not None else "; git unavailable, existence checked on disk only")))


# ======================================================== local server + browser

HARNESS ="""<!doctype html><html><head><meta charset="utf-8"><title>verify harness</title>
<style>html,body{{margin:0;padding:0;overflow:hidden;background:#fff}}
iframe{{border:0;display:block;width:{w}px;height:{h}px}}</style></head>
<body><iframe id="f" src="{src}" width="{w}" height="{h}"></iframe>{script}</body></html>"""

MEASURE_JS = """<script>
(function(){
 var f=document.getElementById('f'), R=document.documentElement;
 function m(){
  try{
   var d=f.contentDocument, w=f.contentWindow, e=d.documentElement, b=d.body;
   R.setAttribute('data-sw', e.scrollWidth);
   R.setAttribute('data-bsw', b ? b.scrollWidth : -1);
   R.setAttribute('data-iw', w.innerWidth);
   R.setAttribute('data-cw', e.clientWidth);
   R.setAttribute('data-sh', e.scrollHeight);
   var off=[], all=b ? b.getElementsByTagName('*') : [];
   for (var i=0;i<all.length && off.length<8;i++){
    var el=all[i], r=el.getBoundingClientRect();
    if (r.width>0 && r.right > w.innerWidth + 1){
     var p=el.parentElement, clipped=false;
     while(p && p!==d.documentElement){
      var ox=w.getComputedStyle(p).overflowX;
      if(ox==='auto'||ox==='scroll'||ox==='hidden'||ox==='clip'){clipped=true;break;}
      p=p.parentElement;
     }
     if(!clipped){
      var c=el.getAttribute('class');
      off.push(el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(c?'.'+c.trim().split(/\\s+/).join('.'):'')+' right='+Math.round(r.right));
     }
    }
   }
   R.setAttribute('data-off', JSON.stringify(off));
   R.setAttribute('data-done','1');
  }catch(x){R.setAttribute('data-err', String(x));}
 }
 f.addEventListener('load', function(){ setTimeout(m, 150); });
})();
</script>"""

CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
          ".js": "application/javascript", ".pdf": "application/pdf", ".csv": "text/csv; charset=utf-8",
          ".json": "application/json", ".xml": "application/xml", ".txt": "text/plain; charset=utf-8",
          ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".webmanifest": "application/manifest+json"}


class LocalServer:
    """Serves a Bundle on 127.0.0.1 with Vercel-style cleanUrls, a stub analytics script,
    and /__verify__/ harness pages. '?__verify_nojs=1' adds CSP script-src 'none'."""

    def __init__(self, bundle: Bundle, analytics_path: str):
        files = bundle.files

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                path = urllib.parse.unquote(u.path)
                status, ctype, body, extra = 200, "text/html; charset=utf-8", b"", {}
                if "__verify_nojs" in q:
                    extra["Content-Security-Policy"] = "script-src 'none'"
                if path == analytics_path:
                    ctype, body = "application/javascript", b"/* verify.py local stub */\n"
                elif path == "/__verify__/ping":
                    body = b"<!doctype html><html><body><p>verify-ping-OK</p></body></html>"
                elif path in ("/__verify__/frame", "/__verify__/probe"):
                    src = q.get("src", ["/"])[0]
                    if q.get("nojs"):
                        src += ("&" if "?" in src else "?") + "__verify_nojs=1"
                    body = HARNESS.format(w=int(q.get("w", ["390"])[0]), h=int(q.get("h", ["900"])[0]),
                                          src=html.escape(src, quote=True),
                                          script=MEASURE_JS if path.endswith("probe") else "").encode()
                else:
                    rel = resolve_path(path, files)
                    if rel is None:
                        status = 404
                        body = files.get("404.html", b"<h1>404</h1>")
                    else:
                        body = files[rel]
                        ctype = CTYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def exe_version(exe: str) -> str:
    try:
        for d in Path(exe).parent.iterdir():
            if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d.name):
                return d.name
    except Exception:  # noqa
        pass
    return "?"


class Browser:
    BASE_FLAGS = ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                  "--disable-extensions", "--disable-background-networking", "--disable-component-update",
                  "--disable-sync", "--no-pings", "--disable-default-apps", "--mute-audio",
                  "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]

    def __init__(self, tmp: Path, prefer: str = "auto"):
        self.tmp = tmp
        self.prefer = prefer
        self.exe = None
        self.name = None
        self.notes: list[str] = []

    def _run(self, exe, extra, url, timeout=90, expect_file: Path | None = None) -> str:
        self.tmp.mkdir(parents=True, exist_ok=True)
        ud = tempfile.mkdtemp(prefix="fv-profile-", dir=str(self.tmp))
        args = [exe, *self.BASE_FLAGS, f"--user-data-dir={ud}", *extra, url]
        try:
            try:
                p = subprocess.run(args, capture_output=True, timeout=timeout)
                out = p.stdout.decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                out = ""
                self.notes.append(f"timeout after {timeout}s: {url}")
            if expect_file is not None:  # Edge may detach from its launcher; wait for the file
                deadline = time.time() + min(timeout, 25)
                last = -1
                while time.time() < deadline:
                    if expect_file.exists():
                        sz = expect_file.stat().st_size
                        if sz > 0 and sz == last:
                            break
                        last = sz
                    time.sleep(0.4)
            return out
        finally:
            for _ in range(10):
                shutil.rmtree(ud, ignore_errors=True)
                if not os.path.exists(ud):
                    break
                time.sleep(0.5)

    def select(self, server: LocalServer) -> bool:
        cands = [("Edge", EDGE), ("Chrome", CHROME)]
        if self.prefer == "chrome":
            cands.reverse()
        elif self.prefer == "edge":
            cands = cands[:1]
        for name, exe in cands:
            if not Path(exe).exists():
                self.notes.append(f"{name} not installed at {exe}")
                continue
            out = self._run(exe, ["--dump-dom"], server.url("/__verify__/ping"), timeout=60)
            if "verify-ping-OK" in out:
                self.exe, self.name = exe, f"{name} {exe_version(exe)}"
                return True
            self.notes.append(f"{name}: --dump-dom produced no output in this environment "
                              f"(launcher detached from stdout); trying the next browser")
        return False

    def dump(self, url, extra=(), timeout=90) -> str:
        return self._run(self.exe, [*extra, "--dump-dom"], url, timeout=timeout)

    def shot(self, url, w, h, png: Path, extra=(), timeout=90) -> bool:
        if png.exists():
            png.unlink()
        self._run(self.exe, [*extra, f"--window-size={w},{h}", "--hide-scrollbars", f"--screenshot={png}"],
                  url, timeout=timeout, expect_file=png)
        return png.exists() and png.stat().st_size > 0


def check_browser(bundle: Bundle, site: dict, R: Report, out_dir: Path, tmp: Path, prefer: str,
                  work_pages=()) -> None:
    R.section("E/F. HEADLESS BROWSER - JS-off render, screenshots, measured mobile overflow")
    server = LocalServer(bundle, site["analytics_path"])
    try:
        br = Browser(tmp, prefer)
        if not br.select(server):
            R.fail("E4", "no headless browser produced a DOM dump; JS-off and mobile checks could not run", br.notes)
            R.fail("F2", "mobile overflow not measured (no browser)")
            return
        R.info("E4", f"browser: {br.name} (headless=new, fresh --user-data-dir under {tmp}, "
                     f"all DNS blocked except 127.0.0.1, served from {server.url('/')})",
                     br.notes + ["JS-off = page served with 'Content-Security-Policy: script-src none' (blocks inline, "
                                 "external and on* scripts). --blink-settings=scriptEnabled=false is not used: in "
                                 "headless=new (Edge/Chrome 152) it makes --dump-dom and --screenshot return nothing."])
        pages = bundle.html_rels()
        jobs = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for rel in pages:
                pg = bundle.page(rel)
                u = pg.url
                jobs[("dom", rel)] = ex.submit(
                    br.dump, server.url(u + ("&" if "?" in u else "?") + "__verify_nojs=1"),
                    ["--virtual-time-budget=5000"])
                for (w, h) in site.get("screenshot_sizes", [[390, 2400], [1280, 2400]]):
                    png = out_dir / f"shot-{slug(rel)}-{w}.png"
                    q = urllib.parse.urlencode({"src": u, "w": w, "h": h, "nojs": 1})
                    jobs[("shot", rel, w)] = ex.submit(br.shot, server.url("/__verify__/frame?" + q), w, h, png)
                w, h = site.get("screenshot_sizes", [[390, 2400]])[0]
                png = out_dir / f"shot-{slug(rel)}-{w}-dark.png"
                q = urllib.parse.urlencode({"src": u, "w": w, "h": h, "nojs": 1})
                jobs[("dark", rel, w)] = ex.submit(br.shot, server.url("/__verify__/frame?" + q), w, h, png,
                                                   ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"])
                for (w, h) in site.get("mobile_viewports", [[320, 900], [375, 812]]):
                    q = urllib.parse.urlencode({"src": u, "w": w, "h": h})
                    jobs[("probe", rel, w)] = ex.submit(
                        br.dump, server.url("/__verify__/probe?" + q),
                        [f"--window-size={max(w, 600)},{h + 40}", "--hide-scrollbars", "--virtual-time-budget=8000"])
        # E: JS-off DOM text == static text
        for rel in pages:
            pg = bundle.page(rel)
            dump = jobs[("dom", rel)].result()
            (out_dir / f"dom-jsoff-{slug(rel)}.html").write_text(dump, encoding="utf-8")
            if "<html" not in dump.lower():
                R.fail("E4", f"{rel}: JS-off render returned no DOM", br.notes[-3:])
                continue
            dt = " ".join(visible_blocks(parse_html(dump).root))
            if dt == pg.text:
                R.ok("E4", f"{rel}: JS-off rendered DOM text == static text ({len(dt):,} chars); nothing JS-dependent")
            else:
                a, b = pg.text.split(" "), dt.split(" ")
                i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
                R.fail("E4", f"{rel}: JS-off rendered text differs from the static file's text",
                       [f"first difference at word {i}:",
                        f"  static : ...{' '.join(a[max(0, i - 8):i + 12])}",
                        f"  browser: ...{' '.join(b[max(0, i - 8):i + 12])}",
                        f"  lengths: static {len(pg.text):,} chars, browser {len(dt):,} chars"])
        shots = []
        for key, fut in jobs.items():
            if key[0] in ("shot", "dark"):
                rel, w = key[1], key[2]
                png = out_dir / (f"shot-{slug(rel)}-{w}.png" if key[0] == "shot" else f"shot-{slug(rel)}-{w}-dark.png")
                if fut.result():
                    shots.append(png.name)
                else:
                    R.warn("E5", f"screenshot failed: {png.name}")
        R.info("E5", f"{len(shots)} JS-off screenshots (390x2400, 1280x2400, 390 dark) in {out_dir}",
               [", ".join(sorted(shots))])
        # F: measured overflow
        heights = {}
        for rel in pages:
            res, f = [], []
            for (w, h) in site.get("mobile_viewports", [[320, 900], [375, 812]]):
                dump = jobs[("probe", rel, w)].result()
                m = re.search(r"<html([^>]*)>", dump)
                attrs = dict(re.findall(r'data-(\w+)="([^"]*)"', m.group(1))) if m else {}
                if attrs.get("sh", "").isdigit():
                    heights[rel] = max(heights.get(rel, 0), int(attrs["sh"]))
                if attrs.get("done") != "1":
                    f.append(f"{w}px: probe did not complete ({attrs.get('err') or 'no data'})")
                    continue
                sw, bsw, iw, cw = (int(attrs.get(k, -1)) for k in ("sw", "bsw", "iw", "cw"))
                off = json.loads(html.unescape(attrs.get("off", "[]")) or "[]")
                line = f"{w}x{h}: scrollWidth {sw} (body {bsw}) vs innerWidth {iw} (clientWidth {cw})"
                if iw != w:
                    f.append(line + f" - viewport is {iw}px, not {w}px: measurement invalid")
                elif max(sw, bsw) > iw:
                    f.append(line + " -> BODY SCROLLS HORIZONTALLY; overflowing: " + (", ".join(off) or "?"))
                else:
                    res.append(line + (f" (warn: > clientWidth)" if max(sw, bsw) > cw else ""))
            (R.fail("F2", f"{rel}: measured horizontal overflow", f + res) if f
             else R.ok("F2", f"{rel}: no horizontal body scroll", res))
        # full-height 390px renders of each work page (charts sit below 2400px), light + dark
        full = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = []
            for rel in [r for r in work_pages if r in pages]:
                hh = min(max(int(heights.get(rel, 2400) * 1.12), 2400), 16000)
                for mode, extra in (("full", []), ("full-dark", ["--force-dark-mode",
                                                                 "--blink-settings=preferredColorScheme=0"])):
                    png = out_dir / f"shot-{slug(rel)}-390-{mode}.png"
                    q = urllib.parse.urlencode({"src": bundle.page(rel).url, "w": 390, "h": hh, "nojs": 1})
                    futs.append((png, ex.submit(br.shot, server.url("/__verify__/frame?" + q), 390, hh, png, extra)))
            for png, fut in futs:
                (full.append(png.name) if fut.result() else R.warn("E5", f"screenshot failed: {png.name}"))
        if full:
            R.info("E5", f"full-height 390px JS-off renders of the long pages (charts, /method, /data): {', '.join(full)}")
    finally:
        server.close()


# ===================================================================== live

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def live_fetch(url: str, timeout=30) -> dict:
    ctx = ssl.create_default_context()  # certificate verification ON
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "filingstrail-verify/1.0"})
    t = time.time()
    try:
        resp = opener.open(req, timeout=timeout)
        status, headers, wire = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        status, headers, wire = e.code, e.headers, e.read()
    enc = (headers.get("Content-Encoding") or "").lower()
    body = gzip.decompress(wire) if enc == "gzip" else wire
    return {"url": url, "status": status, "headers": headers, "wire": wire, "body": body, "enc": enc or "identity",
            "ms": int((time.time() - t) * 1000), "cookies": headers.get_all("Set-Cookie") or []}


def tls_info(host: str) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=20) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            c = ss.getpeercert()
            flat = lambda x: ", ".join("=".join(p) for rdn in x for p in rdn)  # noqa
            na = ssl.cert_time_to_seconds(c["notAfter"])
            return {"subject": flat(c.get("subject", ())), "issuer": flat(c.get("issuer", ())),
                    "notAfter": c["notAfter"], "days_left": int((na - time.time()) / 86400),
                    "san": [v for k, v in c.get("subjectAltName", ()) if k == "DNS"], "tls": ss.version()}


def run_live(base: str, works: list[dict], site: dict, R: Report, dist: Path, out_dir: Path,
             pages: dict | None = None, sources: Sources | None = None) -> None:
    base = base.rstrip("/")
    sp = urllib.parse.urlsplit(base)
    apex = sp.hostname
    www = "www." + apex if not apex.startswith("www.") else apex
    R.section(f"LIVE: {base}")
    got: dict[str, dict] = {}

    def get(path_or_url, cid="L1"):
        u = path_or_url if "://" in path_or_url else base + path_or_url
        try:
            r = live_fetch(u)
        except Exception as e:  # noqa
            R.fail(cid, f"GET {u} failed: {type(e).__name__}: {e}")
            return None
        got[u] = r
        return r

    ctype_ok = {"html": ("text/html",), "pdf": ("application/pdf",), "csv": ("text/csv", "application/csv"),
                "txt": ("text/plain",), "js": ("application/javascript", "text/javascript"),
                "xml": ("application/xml", "text/xml")}
    files: dict[str, bytes] = {}
    page_map = {"/": site.get("root_page", "index.html")}
    for w in works:
        page_map[w["url"]] = w["page"]
    if pages:
        page_map[pages["method"]["url"]] = pages["method"]["page"]
        page_map[pages["data"]["url"]] = pages["data"]["page"]
    for path, rel in page_map.items():
        r = get(path)
        if not r:
            continue
        ct = (r["headers"].get("Content-Type") or "").lower()
        good = r["status"] == 200 and ct.startswith(ctype_ok["html"])
        (R.ok if good else R.fail)("L1", f"GET {path} -> {r['status']} {ct} ({r['enc']}, {len(r['wire']):,} B wire)")
        if r["status"] == 200:
            files[rel] = r["body"]
    extra_paths = []
    for w in works:
        if w["page"] in files:
            pg = Page(w["page"], files[w["page"]])
            for a in pg.nodes("a"):
                h = a.get("href") or ""
                if h.startswith("/") and h.lower().split("?")[0].endswith((".pdf", ".csv")):
                    extra_paths.append(h.split("#")[0])
    if not any(p.lower().endswith(".pdf") for p in extra_paths):
        R.fail("L1", "no PDF link found on the live report page")
    if pages:
        extra_paths += [pages["data"]["manifest_url"], "/sitemap.xml"]
    for path in dict.fromkeys(extra_paths + ["/robots.txt", site["analytics_path"]]):
        r = get(path)
        if not r:
            continue
        ct = (r["headers"].get("Content-Type") or "").lower()
        kind = "pdf" if path.endswith(".pdf") else "csv" if path.endswith(".csv") else \
            "txt" if path.endswith(".txt") else "xml" if path.endswith(".xml") else "js"
        good = r["status"] == 200 and ct.startswith(ctype_ok[kind])
        if kind == "pdf":
            good = good and r["body"].startswith(b"%PDF-")
        hint = " (enable Web Analytics for the Vercel project)" if kind == "js" and r["status"] != 200 else ""
        (R.ok if good else R.fail)("L1", f"GET {path} -> {r['status']} {ct} ({r['enc']}, {len(r['wire']):,} B wire){hint}")
        if r["status"] == 200 and kind != "js":
            files[path.lstrip("/")] = r["body"]
    r404 = get("/__verify_no_such_page__")
    if r404:
        local404 = dist / "404.html"
        custom = (local404.exists() and r404["body"] == local404.read_bytes()) or \
            b"not found" in r404["body"].lower()
        (R.ok if r404["status"] == 404 else R.warn)(
            "L1", f"GET /__verify_no_such_page__ -> {r404['status']} "
                  f"({'custom 404.html served' if custom else 'body is not the site 404 page'})")
        if pages and r404["status"] == 404 and b"<html" in r404["body"][:4096].lower():
            files["404.html"] = r404["body"]  # the served 404 page gets the masthead/footer checks too
    # redirects
    for src, want_codes, want_loc in [(f"http://{apex}/", (301, 308), f"https://{apex}/"),
                                      (f"https://{www}/", (301, 302, 307, 308), f"https://{apex}/"),
                                      (f"http://{www}/", (301, 302, 307, 308), None)]:
        r = get(src, "L2")
        if not r:
            continue
        loc = r["headers"].get("Location") or ""
        good = r["status"] in want_codes and (loc.startswith("https://") if want_loc is None else loc.rstrip("/") == want_loc.rstrip("/"))
        msg = f"{src} -> {r['status']} Location: {loc or '(none)'}"
        if good and r["status"] in (302, 307):
            R.warn("L2", msg + " (temporary redirect; make it permanent 308 in the Vercel domain settings)")
        else:
            (R.ok if good else R.fail)("L2", msg + ("" if good else f" (expected {want_codes} -> {want_loc or 'https://...'})"))
    # cookies
    ck = [f"{u}: {c}" for u, r in got.items() for c in r["cookies"]]
    (R.fail("L3", "Set-Cookie headers present", ck) if ck else
     R.ok("L3", f"no Set-Cookie header on any of {len(got)} responses"))
    # TLS
    for host in (apex, www):
        try:
            t = tls_info(host)
            line = f"{host}: {t['tls']}; subject {t['subject']}; issuer {t['issuer']}; notAfter {t['notAfter']} ({t['days_left']} days); SAN {t['san']}"
            (R.ok if t["days_left"] > 14 else R.warn)("L4", line)
        except Exception as e:  # noqa
            R.fail("L4", f"{host}: TLS handshake / verification failed: {type(e).__name__}: {e}")
    # transfer sizes per page
    ar = got.get(base + site["analytics_path"])
    for path, rel in page_map.items():
        r = got.get(base + path)
        if not r or rel not in files:
            continue
        pg = Page(rel, files[rel])
        subs = [(u, k) for u, k in page_subresources(pg, None, site["analytics_path"])
                if u.startswith("/") and u.split("?")[0] != site["analytics_path"]]
        wire, raw, parts = len(r["wire"]), len(r["body"]), [f"html {len(r['wire']):,}/{len(r['body']):,}"]
        for u, _ in subs:
            s = got.get(base + u) or get(u)
            if s and s["status"] == 200:
                wire += len(s["wire"])
                raw += len(s["body"])
                parts.append(f"{u} {len(s['wire']):,}/{len(s['body']):,}")
                p = urllib.parse.urlsplit(u).path
                if p.lower().endswith(".css"):  # linked stylesheets feed the print/halo/alignment checks below
                    files.setdefault(p.lstrip("/"), s["body"])
        if ar and ar["status"] == 200:
            wire += len(ar["wire"])
            raw += len(ar["body"])
            parts.append(f"analytics {len(ar['wire']):,}/{len(ar['body']):,}")
        (R.ok if raw < site.get("page_weight_limit_bytes", 300000) else R.fail)(
            "L5", f"{path}: {wire:,} B transferred ({r['enc']}), {raw:,} B decoded", ["wire/decoded: " + "; ".join(parts)])
    # same bytes as the verified dist?
    if dist.exists():
        for rel, data in files.items():
            p = dist / rel
            if p.exists():
                same = hashlib.sha256(p.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
                (R.ok if same else R.warn)("L6", f"live {rel} {'==' if same else '!='} local dist/{rel} (sha256)")
    # A, B, C on the live bytes (same code paths as dist mode)
    bundle = Bundle(files, base + " (live)")
    pdfs = [r for r in files if r.lower().endswith(".pdf")]
    check_A(bundle, works, site, R, pdf_rels_override=pdfs)
    check_B(bundle, works, R, out_dir, label="live")
    check_C(bundle, site, R)
    # N (masthead, work list, footers, print, sitemap), M (/method) and D (/data) on the live bytes
    if not pages or sources is None:
        R.fail("N1", "checks/pages.json missing: /method, /data and masthead checks not run on the live site")
        return
    for cid, fn in (("N1", lambda: check_site_nav(bundle, works, site, pages, R)),
                    ("M1", lambda: check_method(bundle, pages, sources, R)),
                    ("D1", lambda: check_data(bundle, pages, sources, R))):
        try:
            fn()
        except Exception as e:  # a crashing check is a failing check
            import traceback
            R.fail(cid, f"check {cid} crashed on the live bytes: {type(e).__name__}: {e}",
                   traceback.format_exc().strip().splitlines()[-6:])


# ===================================================================== main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="filingstrail.com deploy gate")
    ap.add_argument("--dist", default=str(DEFAULT_DIST), help="deploy root to verify (default: site/dist)")
    ap.add_argument("--site-json", default=str(HERE / "site.json"), help="generator config (for repo_url)")
    ap.add_argument("--checks", default=str(DEFAULT_CHECKS), help="directory with <work>.json and site.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="reports and screenshots (default: site/checks/out)")
    ap.add_argument("--allow-missing-repo", action="store_true", help="downgrade 'repo_url not set' to a warning")
    ap.add_argument("--no-browser", action="store_true", help="skip headless-browser checks (run cannot PASS)")
    ap.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    ap.add_argument("--tmp", default=os.environ.get("VERIFY_TMP") or tempfile.gettempdir(),
                    help="parent dir for fresh browser profiles (deleted after each run)")
    ap.add_argument("--live", metavar="URL", help="post-deploy mode against a live origin")
    ap.add_argument("--repo", default=str(HERE.parent),
                    help="repository root holding adv-report/ and archive/ (read-only; default: ..)")
    ap.add_argument("--content", default=str(HERE / "content"), help="authored page copy (content/*.md)")
    ap.add_argument("--stats-log", default=str(HERE / "build" / "adv" / "report_stats.log"),
                    help="report_stats.py output for M3 (optional; M3 is skipped without it)")
    a = ap.parse_args(argv)

    checks_dir, out_dir, dist = Path(a.checks).resolve(), Path(a.out).resolve(), Path(a.dist).resolve()
    a.tmp = str(Path(a.tmp).resolve())
    site = json.loads((checks_dir / "site.json").read_text(encoding="utf-8"))
    works = load_works(checks_dir)
    pages = load_pages(checks_dir)
    src = Sources(Path(a.repo).resolve(), Path(a.content).resolve(), Path(a.stats_log).resolve())
    out_dir.mkdir(parents=True, exist_ok=True)
    R = Report()
    t0 = time.time()
    R.out(f"filingstrail.com deploy gate - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    R.out(f"works: {', '.join(w['work'] + ' (' + w['_file'] + ')' for w in works)}; "
          f"site pages: {'checks/pages.json' if pages else 'checks/pages.json MISSING'}")
    R.out(f"sources (read-only): repo {src.repo}; content {src.content}; stats log {src.stats_log}")

    if a.live:
        run_live(a.live, works, site, R, dist, out_dir, pages, sources=src)
        report_name = "live-report.txt"
    else:
        for old in list(out_dir.glob("shot-*.png")) + list(out_dir.glob("dom-jsoff-*.html")) + \
                list(out_dir.glob("pdf-*-p*.png")):
            old.unlink()
        if not dist.exists() or not any(dist.iterdir()):
            R.fail("--", f"dist directory {dist} does not exist or is empty - nothing to verify")
            return finish(R, out_dir, "verify-report.txt", t0)
        bundle = Bundle.from_dist(dist)
        R.out(f"dist: {dist}  ({len(bundle.files)} files, {sum(map(len, bundle.files.values())):,} bytes; "
              f"'.vercel' ignored)")
        steps = [("A", lambda: check_A(bundle, works, site, R)),
                 ("B", lambda: check_B(bundle, works, R, out_dir)),
                 ("C", lambda: check_C(bundle, site, R)),
                 ("D", lambda: check_D(bundle, site, R)),
                 ("E", lambda: check_E_static(bundle, site, R)),
                 ("F", lambda: check_F_static(bundle, R)),
                 ("G", lambda: check_G(bundle, site, R)),
                 ("H", lambda: check_H(bundle, works, site, Path(a.site_json), a.allow_missing_repo, R)),
                 ("I", lambda: check_I(bundle, works, R, out_dir)),
                 ("J", lambda: check_J(bundle, works, R)),
                 ("K", lambda: check_K(bundle, works, site, R))]
        if pages is None:
            steps.append(("N", lambda: R.fail("N1", f"{checks_dir / 'pages.json'} missing: masthead, /method and "
                                                    f"/data expectations cannot be checked")))
        else:
            steps += [("N1", lambda: check_site_nav(bundle, works, site, pages, R)),
                      ("M1", lambda: check_method(bundle, pages, src, R)),
                      ("D1", lambda: check_data(bundle, pages, src, R)),
                      ("K2", lambda: check_halo(bundle, works, pages, R)),
                      ("H2", lambda: check_repo_links(bundle, site, src, R, works))]
        for cid, fn in steps:
            try:
                fn()
            except Exception as e:  # a crashing check is a failing check
                import traceback
                R.fail(cid, f"check {cid} crashed: {type(e).__name__}: {e}",
                       traceback.format_exc().strip().splitlines()[-6:])
        if a.no_browser:
            R.section("E/F. HEADLESS BROWSER")
            R.skip("E4", "JS-off render comparison skipped (--no-browser)")
            R.skip("F2", "measured mobile overflow skipped (--no-browser)")
        else:
            long_pages = [w["page"] for w in works] + ([pages["method"]["page"], pages["data"]["page"]] if pages else [])
            try:
                check_browser(bundle, site, R, out_dir, Path(a.tmp), a.browser, long_pages)
            except Exception as e:  # noqa
                import traceback
                R.fail("E4", f"browser checks crashed: {type(e).__name__}: {e}",
                       traceback.format_exc().strip().splitlines()[-6:])
        report_name = "verify-report.txt"
    return finish(R, out_dir, report_name, t0)


def finish(R: Report, out_dir: Path, name: str, t0: float) -> int:
    fails, warns, skips = R.count("FAIL"), R.count("WARN"), R.blocking_skips()
    opt = R.count("SKIP") - skips
    R.out("")
    R.out("=" * 78)
    R.out(f"SUMMARY  PASS {R.count('PASS')}   FAIL {fails}   WARN {warns}   SKIP {skips + opt}"
          + (f" ({opt} optional)" if opt else "") + f"   INFO {R.count('INFO')}   ({time.time() - t0:.1f}s)")
    if fails:
        R.out("FAILED checks:")
        for s, c, m in R.rows:
            if s == "FAIL":
                R.out(f"  {c:<4} {ascii_ctx(m, 150)}")
    verdict = "GATE: FAIL - do not deploy" if fails else \
        ("GATE: INCOMPLETE - required checks were skipped; not a deploy PASS" if skips else
         "GATE: PASS" + (" (with warnings)" if warns else ""))
    R.out(verdict)
    R.out("=" * 78)
    try:
        (out_dir / name).write_text("\n".join(R.lines) + "\n", encoding="utf-8")
        print(f"(report written to {out_dir / name})")
    except Exception:  # noqa
        pass
    return 1 if (fails or skips) else 0


if __name__ == "__main__":
    sys.exit(main())
