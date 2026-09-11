#!/usr/bin/env python3
"""Regenerate the ADV report's two charts as inline-ready SVG fragments.

Nothing in this file draws a chart. It runs the frozen research code as-is:

  adv_new_registrants.main()   writes the firm-level CSV and the no-website chart
  report_stats.py              prints every figure in REPORT.md and calls
                               headcount_chart() itself

with matplotlib's Figure.savefig redirected to SVG under site/build/adv/raw/.
Nothing is written into adv-report/ (checked before/after by mtime and git status).
The raw SVGs are then post-processed into fragments that can be inlined into one
HTML page, and everything is cross-checked against the report's numbers gate.

    python site/charts.py           full run; several minutes, because section 8
                                    of report_stats.py reads archive/raw
    python site/charts.py --reuse   skip the regeneration: re-process the raw SVGs
                                    and re-run every check on the existing build

Outputs
  site/assets/adv/new_registrants.svg           inline-ready fragment (deployed)
  site/assets/adv/conversion_by_headcount.svg   inline-ready fragment (deployed)
  site/assets/adv/new_registrants.csv           validated firm-level CSV (deployed)
  site/build/adv/raw/*.svg                      matplotlib's own SVG output
  site/build/adv/new_registrants.csv            CSV as written by main()
  site/build/adv/adv_new_registrants.log        stdout of main()
  site/build/adv/report_stats.log               stdout of report_stats.py
  site/build/adv/preview/index.html             light/dark visual check page

site/assets/ is only written when every check passes. Exit code 1 on any failure.
"""

import sys

# Importing the research code must not write adv-report/__pycache__. Set before
# anything imports it (report_stats.py imports adv_new_registrants.py itself).
sys.dont_write_bytecode = True

import argparse
import contextlib
import importlib.util
import io
import os
import re
import runpy
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "none"         # text stays <text>: small, selectable
matplotlib.rcParams["svg.hashsalt"] = "filingstrail"  # deterministic ids
matplotlib.rcParams["font.family"] = ["Arial", "DejaVu Sans"]
import matplotlib.figure  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

SITE = Path(__file__).resolve().parent
REPO = SITE.parent
ADV = REPO / "adv-report"
ARCHIVE = REPO / "archive"
BUILD = SITE / "build" / "adv"
RAW = BUILD / "raw"
FRAGMENTS = BUILD / "fragments"
PREVIEW = BUILD / "preview"
ASSETS = SITE / "assets" / "adv"

CURRENT = ADV / "data" / "ia09012026-registered.zip"
PRIOR = ADV / "data" / "ia09022025.xlsx"

# chart stem -> id prefix. Both fragments are inlined into the same page.
CHARTS = {"new_registrants": "nr-", "conversion_by_headcount": "hc-"}

# ------------------------------------------------------------ the numbers gate
# The user's list, verbatim in substance. Every row must agree with what the
# frozen code prints today.
GATE = {
    "window": ("2025-08-01", "2026-08-31", 13),
    "entered": 1693, "exited": 903, "net": 790,
    "cohort": 628, "cohort_asof": "2025-07-31",
    "validated": 1689, "validated_pct": "99.8",
    "reregistrations": 4,
    "nosite_validated_pct": "15.9",
    "site_no": (154, "39.6"),    # no website at t0: n, converted %
    "site_yes": (474, "32.1"),   # had a website at t0
    # label on the chart, label in report_stats.py, n, converted %, deregistered %
    "bands": [("1", "1 employee", 128, "25.8", "32.8"),
              ("2-5", "2-5 employees", 265, "44.5", "18.1"),
              ("6-20", "6-20 employees", 134, "31.3", "17.2"),
              ("21+", "21+ employees", 101, "19.8", "4.0")],
    "z_headcount": ("3.58", "0.0003"),
    "z_website": ("1.72", "0.086"),
    "z_dereg": ("3.25", "0.0012"),
}

# Figures on the no-website chart that are not gate rows. Each is asserted to
# appear verbatim in the current REPORT.md before it is used as an expectation.
REPORT_BANDS = [("$0 reported", "32.4", "32.4% among the firms reporting none"),
                ("<$25M", "13.0", "13.0% under $25M"),
                ("$25-100M", "12.6", "12.6% at $25–100M"),
                ("$100-500M", "12.6", "12.6% at $100–500M"),
                ("$500M-1B", "7.2", "7.2% at $500M–1B"),
                (">$1B", "11.1", "11.1% above $1B")]
REPORT_BASELINE = ("8.1", "That is 8.1%")

FORBIDDEN = ["September to September", "twelve months", "1,573", "92.9%",
             "120 re-registrations", "16.2%"]

# ------------------------------------------------------ chart color interface
# matplotlib literal -> (CSS variable, light value). The light value is both the
# literal written first and the var() fallback.
COLORS = {
    "#000000": ("--chart-ink", "#1a1a1a"),
    "#444444": ("--chart-ink-2", "#444444"),
    "#666666": ("--chart-muted", "#666666"),
    "#7a8aa0": ("--chart-bar", "#7a8aa0"),
    "#c05746": ("--chart-alert", "#c05746"),
    "#3e5a78": ("--chart-key", "#3e5a78"),
}
DARK = {"--chart-ink": "#e6e3de", "--chart-ink-2": "#bdb9b2", "--chart-muted": "#a29e97",
        "--chart-bar": "#8595ab", "--chart-alert": "#dd7866", "--chart-key": "#a9c3e0"}
COLOR_PROPS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color", "color")
FONT_STACK = 'Arial, "Helvetica Neue", Helvetica, "Liberation Sans", sans-serif'

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
HREF_ATTRS = ("href", f"{{{XLINK_NS}}}href")
HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
ANY_HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b")  # not &#10; char refs
URL_REF = re.compile(r"url\(#([^)\s]+)\)")

CHECKS = []  # (group, check, expected, got, ok)


def check(group, name, expected, got, ok=None):
    ok = (str(expected) == str(got)) if ok is None else bool(ok)
    CHECKS.append((group, name, str(expected), str(got), ok))
    return ok


class Fail(SystemExit):
    """A condition this script refuses to guess past."""

    def __init__(self, msg):
        super().__init__(f"\nFAIL: {msg}")


# ------------------------------------------------------------ savefig redirect

SAVED = []
_original_savefig = matplotlib.figure.Figure.savefig


def _savefig_as_svg(self, fname, *args, **kwargs):
    """Any savefig from the research code lands in build/adv/raw/<stem>.svg."""
    if args:
        raise Fail(f"unexpected positional savefig arguments {args!r}")
    if not isinstance(fname, (str, os.PathLike)):
        raise Fail(f"savefig target is not a path: {fname!r}")
    stem = Path(os.fspath(fname)).stem
    target = RAW / f"{stem}.svg"
    for k in ("dpi", "format", "transparent", "metadata"):
        kwargs.pop(k, None)
    _original_savefig(self, target, format="svg", transparent=True,
                      metadata={"Date": None}, **kwargs)
    SAVED.append(stem)
    print(f"[charts] savefig({os.fspath(fname)!r}) -> {target.relative_to(REPO)}",
          file=sys.stderr, flush=True)


matplotlib.figure.Figure.savefig = _savefig_as_svg


# ------------------------------------------------------------ running the code

class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


@contextlib.contextmanager
def tee_stdout(log_path):
    with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
        with contextlib.redirect_stdout(Tee(sys.stdout, fh)):
            yield


def run_new_registrants():
    """Step 3: adv_new_registrants.main() with absolute --out under build/."""
    spec = importlib.util.spec_from_file_location(
        "adv_new_registrants", ADV / "adv_new_registrants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    saved_argv, prev = sys.argv, os.getcwd()
    sys.argv = [str(ADV / "adv_new_registrants.py"),
                "--current", str(CURRENT), "--prior", str(PRIOR),
                "--out", str(BUILD / "new_registrants")]
    try:
        os.chdir(BUILD)  # any stray relative write lands in build/, never adv-report/
        with tee_stdout(BUILD / "adv_new_registrants.log"):
            mod.main()
    finally:
        os.chdir(prev)
        sys.argv = saved_argv
        plt.close("all")


def run_report_stats():
    """Step 4: report_stats.py as a script, from inside adv-report/ (relative data/ paths)."""
    saved_argv, prev = sys.argv, os.getcwd()
    sys.argv = [str(ADV / "report_stats.py")]
    try:
        os.chdir(ADV)
        with tee_stdout(BUILD / "report_stats.log"):
            runpy.run_path(str(ADV / "report_stats.py"), run_name="__main__")
    finally:
        os.chdir(prev)
        sys.argv = saved_argv
        plt.close("all")


# ------------------------------------------------------------ read-only proof

def snapshot(*roots):
    out = {}
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[p.relative_to(REPO).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def git_status():
    r = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--",
                        "adv-report", "archive", "CLAUDE.md"],
                       capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"git status failed: {r.stderr.strip()}")
    return r.stdout.strip()


# ------------------------------------------------------------ SVG processing

def tag(el):
    return el.tag.rsplit("}", 1)[-1]


def parse_style(s):
    decls = []
    for part in (s or "").split(";"):
        if part.strip():
            k, _, v = part.partition(":")
            decls.append([k.strip(), v.strip()])
    return decls


def css(decls):
    return "; ".join(f"{k}: {v}" for k, v in decls)


def norm_hex(h):
    h = h.lower()
    return "#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h


def color_pair(prop, value, where):
    h = norm_hex(value)
    if h not in COLORS:
        raise Fail(f"{where}: {prop}: {value} is not in the chart color interface. "
                   "Stopping rather than guessing a mapping.")
    var, light = COLORS[h]
    return [[prop, light], [prop, f"var({var}, {light})"]]


def is_invisible(el):
    """Figure/axes background patch: fill none, no stroke (transparent=True)."""
    if tag(el) not in ("path", "rect"):
        return False
    d = dict(parse_style(el.get("style")))
    fill = d.get("fill", el.get("fill"))
    stroke = d.get("stroke", el.get("stroke"))
    return fill == "none" and stroke in (None, "none")


def restyle(el, where):
    """Colors -> literal + var() pairs; font-family -> the site stack."""
    decls = parse_style(el.get("style"))
    for prop in COLOR_PROPS + ("font-family",):  # presentation attributes -> style
        v = el.get(prop)
        if v is not None:
            del el.attrib[prop]
            decls.append([prop, v.strip()])
    out = []
    for k, v in decls:
        if k in COLOR_PROPS:
            if HEX.fullmatch(v):
                out += color_pair(k, v, where)
            elif v == "none" or URL_REF.fullmatch(v):
                out.append([k, v])
            else:
                raise Fail(f"{where}: {k}: {v!r} is not a hex color; not guessing")
        elif k == "font-family":
            out.append([k, FONT_STACK])
        elif k == "font":
            raise Fail(f"{where}: unexpected font shorthand {v!r}")
        else:
            out.append([k, v])
    if tag(el) == "text" and not any(k == "fill" for k, _ in out):
        # matplotlib omits fill for black text; SVG's default would stay black in dark mode
        out += color_pair("fill", "#000000", where)
    if el.get("d") is not None:  # matplotlib breaks path data across lines
        el.set("d", " ".join(el.get("d").split()))
    t = el.get("transform")
    if t is not None and re.fullmatch(r"rotate\(-?0(?:\.0+)?(?: [-\d.e]+ [-\d.e]+)?\)", t):
        del el.attrib["transform"]  # matplotlib writes rotate(-0 x y) on unrotated text
    if out:
        el.set("style", css(out))
    elif "style" in el.attrib:
        del el.attrib["style"]


def rewrite_ids(root, prefix, stem):
    ids = [el.get("id") for el in root.iter() if el.get("id") is not None]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise Fail(f"{stem}: duplicate ids in matplotlib output: {sorted(dup)}")
    referenced = set()
    for el in root.iter():
        for k, v in el.attrib.items():
            referenced.update(URL_REF.findall(v))
            if k in HREF_ATTRS and v.startswith("#"):
                referenced.add(v[1:])
    dangling = referenced - set(ids)
    if dangling:
        raise Fail(f"{stem}: references to missing ids: {sorted(dangling)}")
    for el in root.iter():
        for k, v in list(el.attrib.items()):
            if k == "id":
                if v in referenced:
                    el.set("id", prefix + v)
                else:
                    del el.attrib["id"]
                continue
            nv = URL_REF.sub(lambda m: f"url(#{prefix}{m.group(1)})", v)
            if k in HREF_ATTRS and nv.startswith("#"):
                nv = "#" + prefix + nv[1:]
            if nv != v:
                el.set(k, nv)
    return len(ids), len(referenced)


def flatten(parent):
    """Unwrap attribute-less <g>, drop empty <g>/<defs>. Order is preserved."""
    kept = []
    for ch in list(parent):
        if tag(ch) not in ("text", "tspan", "clipPath"):
            flatten(ch)
        if tag(ch) in ("g", "defs") and len(ch) == 0:
            continue
        if tag(ch) == "g" and not ch.attrib:
            kept.extend(list(ch))
        else:
            kept.append(ch)
    parent[:] = kept


def reflow(el):
    """One element per line; never touches text content."""
    if tag(el) in ("text", "tspan", "title", "desc"):
        return
    if len(el):
        if el.text is None or not el.text.strip():
            el.text = "\n"
        for ch in el:
            reflow(ch)
            if ch.tail is None or not ch.tail.strip():
                ch.tail = "\n"


def report_alt_texts():
    text = (ADV / "REPORT.md").read_text(encoding="utf-8")
    alts = dict((m.group(2), m.group(1)) for m in
                re.finditer(r"!\[([^\]]*)\]\(([a-z_]+)\.png\)", text))
    for stem in CHARTS:
        if stem not in alts:
            raise Fail(f"REPORT.md has no image line for {stem}.png")
    return alts


def process_svg(stem, prefix, alt):
    src = (RAW / f"{stem}.svg").read_text(encoding="utf-8")
    root = ET.fromstring(src)  # the parser drops the XML declaration, DOCTYPE, comments
    if tag(root) != "svg":
        raise Fail(f"{stem}: root is <{tag(root)}>")
    viewbox = root.get("viewBox")
    if not viewbox:
        raise Fail(f"{stem}: no viewBox")

    root_decls = []
    removed = {"metadata": 0, "style": 0, "background": 0}
    for parent in list(root.iter()):
        for ch in list(parent):
            t = tag(ch)
            if t in ("script", "foreignObject", "image"):
                raise Fail(f"{stem}: unexpected <{t}> in matplotlib output")
            if t == "metadata":
                parent.remove(ch)
                removed["metadata"] += 1
            elif t == "style":
                # matplotlib's global *{stroke-linejoin: round; stroke-linecap: butt}
                # would become a page-wide rule once inlined; the same properties
                # inherit from the root instead.
                m = re.fullmatch(r"\s*\*\s*\{([^}]*)\}\s*", ch.text or "")
                if not m:
                    raise Fail(f"{stem}: unexpected <style> content {ch.text!r}")
                root_decls += parse_style(m.group(1))
                parent.remove(ch)
                removed["style"] += 1
            elif is_invisible(ch):
                parent.remove(ch)
                removed["background"] += 1

    for el in root.iter():
        if el is not root:
            restyle(el, f"{stem} <{tag(el)} id={el.get('id')}>")
    n_ids, n_refs = rewrite_ids(root, prefix, stem)
    flatten(root)

    root.attrib.clear()
    root.set("class", "chart")
    root.set("viewBox", viewbox)
    root.set("role", "img")
    root.set("aria-label", alt)
    root.set("style", css(root_decls + color_pair("fill", "#000000", f"{stem} <svg>")))
    reflow(root)
    out = ET.tostring(root, encoding="unicode") + "\n"
    ET.fromstring(out)  # well-formed, or this raises
    return out, removed, n_ids, n_refs


def svg_texts(svg_text):
    """[(context, text)] in document order; context from matplotlib's group ids."""
    out = []

    def walk(el, ctx):
        gid = el.get("id") or ""
        for c in ("xtick_", "ytick_", "legend_"):
            if gid.startswith(c):
                ctx = c[:-1]
        if tag(el) == "text":
            out.append((ctx, "".join(el.itertext())))
        for ch in el:
            walk(ch, ctx)

    walk(ET.fromstring(svg_text), "other")
    return out


def audit_fragment(stem, prefix, svg):
    g = f"svg:{stem}"
    root = ET.fromstring(svg)
    check(g, "well-formed (xml.etree parse)", "ok", "ok")
    check(g, "root class / viewBox / no width,height",
          "chart / present / none",
          f"{root.get('class')} / {'present' if root.get('viewBox') else 'missing'} / "
          f"{'none' if not ({'width', 'height'} & set(root.attrib)) else 'present'}")
    for bad in ("<?xml", "<!DOCTYPE", "<metadata", "<!--", "<style", "<script"):
        check(g, f"no {bad}", 0, svg.count(bad))

    # every hex color must sit in a literal + var() pair from the interface
    pair = re.compile(r"(fill|stroke|stop-color|flood-color|lighting-color|color):\s*"
                      r"(#[0-9a-f]{6});\s*\1:\s*var\((--chart-[a-z0-9-]+),\s*\2\)")
    pairs = pair.findall(svg)
    wrong = sorted({(v, h) for _, h, v in pairs
                    if (v, h) not in {(vv, lt) for vv, lt in COLORS.values()}})
    check(g, "var()/fallback pairs match the interface", "[]", wrong)
    leftover = ANY_HEX.findall(pair.sub("", svg))
    check(g, "hex colors outside a literal+var() pair", "[]", sorted(set(leftover)))
    check(g, "var() pairs written", "> 0", len(pairs), ok=len(pairs) > 0)
    vars_used = sorted(set(re.findall(r"var\((--chart-[a-z0-9-]+)", svg)))
    check(g, "CSS variables used", "subset of interface", ", ".join(vars_used),
          ok=set(vars_used) <= set(DARK))
    fams = {v for el in root.iter() for k, v in parse_style(el.get("style"))
            if k == "font-family"}
    check(g, "font-family declarations", FONT_STACK, " | ".join(sorted(fams)) or "(none)")
    texts = [el for el in root.iter() if tag(el) == "text"]
    no_fill = [el for el in texts if not any(k == "fill" for k, _ in parse_style(el.get("style")))]
    check(g, "<text> without explicit fill", 0, len(no_fill))

    ids = [el.get("id") for el in root.iter() if el.get("id") is not None]
    check(g, f"ids all prefixed {prefix!r}", "[]", [i for i in ids if not i.startswith(prefix)])
    check(g, "duplicate ids", "[]", sorted({i for i in ids if ids.count(i) > 1}))
    refs = set()
    for el in root.iter():
        for k, v in el.attrib.items():
            refs.update(URL_REF.findall(v))
            if k in HREF_ATTRS and v.startswith("#"):
                refs.add(v[1:])
    check(g, "unresolved references", "[]", sorted(refs - set(ids)))
    check(g, "unreferenced ids", "[]", sorted(set(ids) - refs))
    for s in FORBIDDEN:
        check(g, f"forbidden string {s!r}", 0, svg.count(s))
    size = len(svg.encode("utf-8"))
    check(g, "size < 60 KB", "< 61,440 bytes", f"{size:,} bytes", ok=size < 60 * 1024)
    return ids, size


# ------------------------------------------------------------ log cross-check

def grab(pattern, text, what):
    m = re.search(pattern, text, re.M)
    if not m:
        raise Fail(f"could not find {what} in the log (pattern {pattern!r})")
    return m.groups()


def fmt_like(value, reference):
    """Round a printed value to the reference's number of decimals."""
    dec = len(reference.split(".")[1]) if "." in reference else 0
    return f"{float(value):.{dec}f}"


def cross_check_stats():
    log = (BUILD / "report_stats.log").read_text(encoding="utf-8")
    alog = (BUILD / "adv_new_registrants.log").read_text(encoding="utf-8")
    report = " ".join((ADV / "REPORT.md").read_text(encoding="utf-8").split())
    G = "stats"

    def in_report(*needles):
        return all(n in report for n in needles)

    w0, w1, months = GATE["window"]
    a, b, c = grab(r"^window \.+ (\S+) to (\S+), (\d+) months$", log, "window")
    check(G, "window (report_stats)", f"{w0} to {w1}, {months} months", f"{a} to {b}, {c} months")
    a, b, c = grab(r"^window: (\S+) to (\S+) \((\d+) months of filings\)$", alog, "window")
    check(G, "window (adv_new_registrants)", f"{w0} to {w1}, {months} months",
          f"{a} to {b}, {c} months")
    check(G, "window (REPORT.md)", "August 1, 2025 to August 31, 2026; thirteen months",
          "present" if in_report("August 1, 2025 to August 31, 2026", "thirteen months")
          else "MISSING", ok=in_report("August 1, 2025 to August 31, 2026", "thirteen months"))

    ent, = grab(r"^entered \.+ ([\d,]+)$", log, "entered")
    ex, = grab(r"^exited \.+ ([\d,]+)$", log, "exited")
    net, = grab(r"^net change \.+ ([+-][\d,]+)$", log, "net")
    exp = f"{GATE['entered']:,} / {GATE['exited']:,} / +{GATE['net']:,}"
    check(G, "entered / exited / net (report_stats)", exp, f"{ent} / {ex} / {net}")
    ent2, = grab(r"^entered \(in current, not in prior\): ([\d,]+)$", alog, "entered")
    ex2, = grab(r"^exited  \(in prior, not in current\): ([\d,]+)$", alog, "exited")
    net2, = grab(r"\(net ([+-][\d,]+)\)$", alog, "net")
    check(G, "entered / exited / net (adv_new_registrants)", exp, f"{ent2} / {ex2} / {net2}")
    s = f"{GATE['entered']:,} entered, {GATE['exited']:,} exited, net +{GATE['net']:,}"
    check(G, "entered / exited / net (REPORT.md)", s, "present" if in_report(s) else "MISSING",
          ok=in_report(s))

    asof, tracked = grab(r"THE PANEL: \$0-AUM COHORT AS OF (\S+), TRACKED (\d+) MONTHS",
                         log, "panel header")
    n0, = grab(r"^cohort size \(\$0 AUM at t0\) \.+ ([\d,]+)$", log, "cohort size")
    check(G, "$0-AUM cohort (report_stats)",
          f"{GATE['cohort']:,} as of {GATE['cohort_asof']}, tracked {months} months",
          f"{n0} as of {asof}, tracked {tracked} months")
    s = f"The roster as of July 31, 2025 had {GATE['cohort']:,} registered advisers reporting $0"
    check(G, "$0-AUM cohort (REPORT.md)", s, "present" if in_report(s) else "MISSING",
          ok=in_report(s))

    cut, k, p = grab(r"^effective date on/after (\S+) \.+ ([\d,]+) \(([\d.]+)%\)$", log,
                     "validated")
    check(G, "validated new registrations (report_stats)",
          f"on/after {w0}: {GATE['validated']:,} of {GATE['entered']:,} ({GATE['validated_pct']}%)",
          f"on/after {cut}: {k} of {ent} ({p}%)")
    cut2, k2, n2, p2 = grab(r"^SEC status effective date on/after (\S+): ([\d,]+) of ([\d,]+) "
                            r"\(([\d.]+)%\)$", alog, "validated")
    check(G, "validated new registrations (adv_new_registrants)",
          f"on/after {w0}: {GATE['validated']:,} of {GATE['entered']:,} ({GATE['validated_pct']}%)",
          f"on/after {cut2}: {k2} of {n2} ({p2}%)")
    s = f"{GATE['validated']:,} of {GATE['entered']:,} ({GATE['validated_pct']}%)"
    check(G, "validated new registrations (REPORT.md)", s,
          "present" if in_report(s) else "MISSING", ok=in_report(s))

    old, _ = grab(r"^older effective date \.+ ([\d,]+) \(([\d.]+)%\)$", log, "older")
    unp, = grab(r"^unparseable \.+ ([\d,]+)$", log, "unparseable")
    check(G, "re-registrations (report_stats)", f"{GATE['reregistrations']} (0 unparseable)",
          f"{old} ({unp} unparseable)")
    old2, = grab(r"^\s+older effective date \(re-registration / status change\): ([\d,]+)$",
                 alog, "older")
    check(G, "re-registrations (adv_new_registrants)", GATE["reregistrations"], old2)
    s = f"The other {GATE['reregistrations']} are re-registrations"
    check(G, "re-registrations (REPORT.md)", s, "present" if in_report(s) else "MISSING",
          ok=in_report(s))

    nsv, = grab(r"^no-website rate, genuine only \.+ ([\d.]+)%$", log, "genuine no-website")
    check(G, "no-website rate, validated only", f"{GATE['nosite_validated_pct']}%", f"{nsv}%")
    s = f"holds without them, at {GATE['nosite_validated_pct']}%"
    check(G, "no-website rate, validated only (REPORT.md)", s,
          "present" if in_report(s) else "MISSING", ok=in_report(s))

    yn, yc = grab(r"^\s+had a website\s+n=\s*(\d+)\s+converted\s+([\d.]+)%", log, "had website")
    nn, nc = grab(r"^\s+not had a website\s+n=\s*(\d+)\s+converted\s+([\d.]+)%", log, "no website")
    check(G, "website split: no site / with one",
          f"{GATE['site_no'][1]}% (n={GATE['site_no'][0]}) vs {GATE['site_yes'][1]}% "
          f"(n={GATE['site_yes'][0]})", f"{nc}% (n={nn}) vs {yc}% (n={yn})")
    s1 = f"{GATE['site_no'][1]}% (n={GATE['site_no'][0]})"
    s2 = f"{GATE['site_yes'][1]}% for firms that had one (n={GATE['site_yes'][0]})"
    check(G, "website split (REPORT.md)", f"{s1} ... {s2}",
          "present" if in_report(s1, s2) else "MISSING", ok=in_report(s1, s2))

    for label, name, n, conv, dereg in GATE["bands"]:
        bn, bc, bd = grab(rf"^\s+{re.escape(name)}\s+n=\s*(\d+)\s+converted\s+([\d.]+)%"
                          rf"\s+dereg\s+([\d.]+)%$", log, name)
        check(G, f"headcount band {label}: n, converted / dereg",
              f"n={n}, {conv}% / {dereg}%", f"n={bn}, {bc}% / {bd}%")
        check(G, f"headcount band {label} (REPORT.md table)", "row present",
              "row present" if re.search(
                  rf"\| {re.escape(label.replace('-', '–'))} \| {n} \| (\*\*)?{re.escape(conv)}%"
                  rf"(\*\*)? \| {re.escape(dereg)}% \|", report) else "MISSING")

    def ztest(header, what, key, rep):
        d, z, p = grab(rf"{re.escape(header)}\n.*\n.*\n\s+difference ([+-][\d.]+)pp\s+"
                       rf"z=([-\d.]+)\s+p=([\d.]+)", log, header)
        gz, gp = GATE[key]
        check(G, f"test: {what}", f"z={gz} p={gp}",
              f"z={z} p={p} (printed) -> p={fmt_like(p, gp)}",
              ok=(fmt_like(z, gz) == gz and fmt_like(p, gp) == gp))
        check(G, f"test: {what} (REPORT.md)", rep, "present" if in_report(rep) else "MISSING",
              ok=in_report(rep))

    ztest("CONVERSION: headcount", "headcount (conversion)", "z_headcount",
          "z = 3.58, p = 0.0003")
    ztest("CONVERSION: website (the null result)", "website (conversion)", "z_website",
          "z = 1.72, p = 0.086")
    ztest("DEREGISTRATION: headcount", "deregistration (headcount)", "z_dereg",
          "z = 3.25, p = 0.0012")

    for s in FORBIDDEN:
        if s in log:
            print(f"  note: report_stats.log contains {s!r} (section 4's filename-cut "
                  "counterfactual). The log is build-only and must never be deployed.")

    # per-band table for the no-website chart (not a gate row; checked against REPORT.md)
    bands = {}
    for m in re.finditer(r"^  (\S+(?: reported)?)\s+n=\s*([\d,]+)\s+([\d.]+)%\s+\(", log, re.M):
        bands[m.group(1)] = (m.group(2), m.group(3))
    base_k, base_p = grab(r"^no website \(Item 1\.I = N\) \.+ ([\d,]+) \(([\d.]+)%\)$", log,
                          "baseline")
    return bands, base_p


# ------------------------------------------------------------ SVG text check

def label_for(rate, n):
    """Whole-number bar label implied by a 1-dp table rate and its n."""
    k = round(float(rate) * n / 100)
    exact = k / n * 100
    if f"{exact:.1f}" != rate:
        raise Fail(f"no integer count reproduces {rate}% of {n}")
    return f"{exact:.0f}%"


def human(iso):
    d = pd.Timestamp(iso)
    return f"{d:%b} {d.day}, {d.year}"


def cross_check_svg_text(raw_svgs, fragments, bands_log, baseline_log):
    report = " ".join((ADV / "REPORT.md").read_text(encoding="utf-8").split())
    w0, w1, _ = GATE["window"]
    for stem, prefix in CHARTS.items():
        g = f"text:{stem}"
        raw = svg_texts(raw_svgs[stem])
        frag = svg_texts(fragments[stem])
        check(g, "text in fragment == text in matplotlib's SVG", f"{len(raw)} strings",
              f"{len(frag)} strings", ok=[t for _, t in raw] == [t for _, t in frag])
        xt = [t for c, t in raw if c == "xtick"]
        other = [t for c, t in raw if c == "other"]
        legend = [t for c, t in raw if c == "legend"]
        bar = [t for t in other if re.fullmatch(r"\d+%", t)]
        if stem == "new_registrants":
            for label, rate, phrase in REPORT_BANDS:
                check(g, f"REPORT.md states {phrase!r}", "present",
                      "present" if phrase in report else "MISSING")
            exp_ticks = []
            for label, rate, _ in REPORT_BANDS:
                n = bands_log.get(label, ("?", "?"))[0]
                exp_ticks += [label, f"n={n}"]
                check(g, f"band {label}: log rate == REPORT.md", f"{rate}%",
                      f"{bands_log.get(label, ('?', '?'))[1]}%")
            check(g, "x tick labels (band, n=) vs report_stats.log", " | ".join(exp_ticks),
                  " | ".join(xt))
            check(g, "$0 band n (REPORT.md: 309 ... reporting exactly $0)", "n=309",
                  xt[1] if len(xt) > 1 else "?",
                  ok=len(xt) > 1 and xt[1] == "n=309" and "309 of the advisers" in report)
            exp_bar = [f"{float(r):.0f}%" for _, r, _ in REPORT_BANDS]
            check(g, "bar labels (whole-number REPORT.md rates)", " ".join(exp_bar), " ".join(bar))
            base = f"all registered advisers: {REPORT_BASELINE[0]}%"
            check(g, "baseline label", base,
                  next((t for t in other if t.startswith("all registered")), "(none)"),
                  ok=base in other and REPORT_BASELINE[1] in report
                  and baseline_log == REPORT_BASELINE[0])
            title = (f"{GATE['entered']:,} firms registered {human(w0)} to {human(w1)}, "
                     "by reported regulatory AUM")
            check(g, "title: count and window", title,
                  next((t for t in other if "firms registered" in t), "(none)"))
        else:
            exp_ticks = []
            for label, _, n, _, _ in GATE["bands"]:
                exp_ticks += [label, f"n={n}"]
            check(g, "x tick labels (band, n=) vs gate", " | ".join(exp_ticks), " | ".join(xt))
            exp_bar = ([label_for(c, n) for _, _, n, c, _ in GATE["bands"]]
                       + [label_for(d, n) for _, _, n, _, d in GATE["bands"]])
            check(g, "bar labels: converted x4 then deregistered x4", " ".join(exp_bar),
                  " ".join(bar))
            title = (f"{GATE['cohort']:,} SEC-registered advisers reporting $0 AUM as of "
                     f"{human(GATE['cohort_asof'])}, tracked to {human(w1)}")
            check(g, "title: cohort, as-of and end dates", title,
                  next((t for t in other if "SEC-registered advisers reporting" in t), "(none)"))
            asof = pd.Timestamp(GATE["cohort_asof"])
            xl = f"employees reported on Form ADV Item 5.A, {asof:%B %Y} roster"
            check(g, "x axis label", xl, next((t for t in other if t.startswith("employees")),
                                              "(none)"))
            gz, gp = GATE["z_headcount"]
            foot = f"z={gz}, p={gp}."
            check(g, "footnote test statistic", foot,
                  next((t[t.find("z="):] for t in other if "z=" in t), "(none)"),
                  ok=any(t.endswith(foot) and "between 1 and 2-5 employees" in t for t in other))
            d21 = f"{float(GATE['bands'][3][4]):.0f}% deregistration rate"
            check(g, f"footnote 21+ band: {d21!r}", "present",
                  "present" if any(d21 in t for t in other) else "MISSING")
            check(g, "legend", "reported assets thirteen months later | deregistered", " | ".join(legend))
        joined = "\n".join(t for _, t in frag)
        for s in FORBIDDEN:
            check(g, f"forbidden string {s!r} in chart text", 0, joined.count(s))


# ------------------------------------------------------------ CSV

def validate_csv():
    path = BUILD / "new_registrants.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    g = "csv"
    check(g, "rows", f"{GATE['entered']:,}", f"{len(df):,}")
    vc = df["_new_registration"].value_counts().to_dict()
    check(g, "_new_registration True / False", f"{GATE['validated']:,} / {GATE['reregistrations']}",
          f"{vc.get('True', 0):,} / {vc.get('False', 0)}")
    vs = df["_site"].value_counts().to_dict()
    check(g, "_site False (no website)", 269, vs.get("False", 0))
    raw = path.read_text(encoding="utf-8")
    for s in FORBIDDEN:
        check(g, f"forbidden string {s!r}", 0, raw.count(s))
    return df


def compare_committed(df_new):
    new_p, old_p = BUILD / "new_registrants.csv", ADV / "new_registrants.csv"
    a, b = new_p.read_bytes(), old_p.read_bytes()
    lines = [f"regenerated {len(a):,} bytes vs committed {len(b):,} bytes"]
    old = pd.read_csv(old_p, dtype=str, keep_default_na=False)
    vc = old["_new_registration"].value_counts().to_dict()
    lines.append(f"committed file: {len(old):,} rows; _new_registration True/False = "
                 f"{vc.get('True', 0):,}/{vc.get('False', 0)}; _site False = "
                 f"{old['_site'].value_counts().get('False', 0)}")
    if a == b:
        lines.append("RESULT: byte-identical")
        return "\n".join(lines)
    if a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"):
        lines.append("RESULT: identical after line-ending normalization")
        return "\n".join(lines)
    cols_new, cols_old = list(df_new.columns), list(old.columns)
    if cols_new != cols_old:
        lines.append(f"columns only in regenerated: {sorted(set(cols_new) - set(cols_old))}")
        lines.append(f"columns only in committed:   {sorted(set(cols_old) - set(cols_new))}")
    common = [c for c in cols_new if c in cols_old]
    sa = df_new[common].sort_values(common).reset_index(drop=True)
    sb = old[common].sort_values(common).reset_index(drop=True)
    if sa.equals(sb) and cols_new == cols_old:
        lines.append("RESULT: same rows, different order")
        return "\n".join(lines)
    key = "Organization CRD#"
    ma = df_new.set_index(key)
    mb = old.set_index(key)
    only_new, only_old = sorted(set(ma.index) - set(mb.index)), sorted(set(mb.index) - set(ma.index))
    lines.append(f"CRDs only in regenerated: {len(only_new)} {only_new[:10]}")
    lines.append(f"CRDs only in committed:   {len(only_old)} {only_old[:10]}")
    both = sorted(set(ma.index) & set(mb.index))
    for c in common:
        if c == key:
            continue
        diff = [i for i in both if ma.at[i, c] != mb.at[i, c]]
        if diff:
            lines.append(f"column {c!r}: {len(diff)} rows differ, e.g. CRD {diff[0]}: "
                         f"{mb.at[diff[0], c]!r} -> {ma.at[diff[0], c]!r}")
    lines.append("RESULT: content differs (details above)")
    return "\n".join(lines)


# ------------------------------------------------------------ preview page

def write_preview(fragments):
    """index.html: fluid page. narrow.html: body fixed at 390 CSS px, because
    headless Edge will not lay out a window narrower than ~492 px."""
    PREVIEW.mkdir(parents=True, exist_ok=True)
    light = ";".join(f"{v}:{lt}" for v, lt in COLORS.values())
    dark = ";".join(f"{k}:{v}" for k, v in DARK.items())
    for name, body_css in (("index.html", ""), ("narrow.html", "width:390px;")):
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>charts preview</title>
<style>
:root{{--bg:#fcfbf9;--fg:#1a1a1a;{light}}}
@media (prefers-color-scheme: dark){{:root{{--bg:#161615;--fg:#e6e3de;{dark}}}}}
html{{background:var(--bg)}}
body{{margin:0;padding:16px;box-sizing:border-box;{body_css}background:var(--bg);color:var(--fg);font:15px/1.5 Georgia,serif}}
main{{max-width:960px;margin:0 auto}}
svg.chart{{display:block;width:100%;height:auto;margin:8px 0 28px}}
</style></head><body><main>
<p>Chart preview (build only, not deployed). Scheme follows prefers-color-scheme.</p>
{fragments['new_registrants']}
{fragments['conversion_by_headcount']}
</main></body></html>
"""
        (PREVIEW / name).write_text(html, encoding="utf-8", newline="\n")
    return PREVIEW / "index.html"


# ------------------------------------------------------------ main

def print_table():
    width = max(len(c[1]) for c in CHECKS)
    group = None
    for grp, name, exp, got, ok in CHECKS:
        if grp != group:
            print(f"\n[{grp}]")
            group = grp
        flag = "PASS" if ok else "FAIL"
        line = f"  {flag}  {name:<{width}}  {got}"
        if not ok:
            line += f"    (expected {exp})"
        print(line)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reuse", action="store_true",
                    help="skip regeneration; re-process existing build outputs")
    args = ap.parse_args()

    for d in (BUILD, RAW, FRAGMENTS):
        d.mkdir(parents=True, exist_ok=True)
    before = snapshot(ADV, ARCHIVE)
    status_before = git_status()
    print(f"adv-report + archive snapshot: {len(before):,} files; git status "
          f"{'clean' if not status_before else 'NOT clean:' + chr(10) + status_before}")

    if not args.reuse:
        for p in list(RAW.glob("*.svg")) + [BUILD / "new_registrants.csv",
                                             BUILD / "report_stats.log",
                                             BUILD / "adv_new_registrants.log"]:
            p.unlink(missing_ok=True)
        print("\n" + "#" * 70 + "\n# adv_new_registrants.main()\n" + "#" * 70)
        run_new_registrants()
        print("\n" + "#" * 70 + "\n# report_stats.py\n" + "#" * 70)
        run_report_stats()
        check("run", "savefig calls redirected to SVG", sorted(CHARTS), sorted(SAVED))

    missing = [p for p in [RAW / f"{s}.svg" for s in CHARTS] + [
        BUILD / "new_registrants.csv", BUILD / "report_stats.log",
        BUILD / "adv_new_registrants.log"] if not p.exists()]
    if missing:
        raise Fail(f"missing build outputs {missing}; run without --reuse")

    print("\n" + "#" * 70 + "\n# post-processing and checks\n" + "#" * 70)
    alts = report_alt_texts()
    raw_svgs, fragments, all_ids, sizes = {}, {}, [], {}
    for stem, prefix in CHARTS.items():
        raw_svgs[stem] = (RAW / f"{stem}.svg").read_text(encoding="utf-8")
        out, removed, n_ids, n_refs = process_svg(stem, prefix, alts[stem])
        fragments[stem] = out
        (FRAGMENTS / f"{stem}.svg").write_text(out, encoding="utf-8", newline="\n")
        print(f"{stem}: raw {len((RAW / f'{stem}.svg').read_bytes()):,} B -> fragment "
              f"{len(out.encode()):,} B; removed {removed}; ids {n_ids} -> kept {n_refs}")
        ids, sizes[stem] = audit_fragment(stem, prefix, out)
        all_ids += ids
    check("svg:both", "ids unique across both fragments", "[]",
          sorted({i for i in all_ids if all_ids.count(i) > 1}))
    check("svg:both", "combined fragment size", "< 120 KB", f"{sum(sizes.values()):,} bytes",
          ok=sum(sizes.values()) < 120 * 1024)

    bands_log, baseline_log = cross_check_stats()
    cross_check_svg_text(raw_svgs, fragments, bands_log, baseline_log)
    df = validate_csv()
    comparison = compare_committed(df)

    after = snapshot(ADV, ARCHIVE)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    check("read-only", "adv-report/ + archive/ files changed (size, mtime)", "[]", changed)
    status_after = git_status()
    # Unchanged by this run, not necessarily empty: a deliberate, uncommitted
    # edit to the research code (e.g. a chart label) is the reason to rerun this.
    # The size/mtime snapshot above is what proves nothing was written.
    check("read-only", "git status --porcelain -- adv-report archive CLAUDE.md (unchanged by this run)",
          status_before or "(empty)", status_after or "(empty)")

    preview = write_preview(fragments)
    print_table()
    print("\n[committed CSV comparison]\n  " + comparison.replace("\n", "\n  "))
    print(f"\npreview page: {preview}")

    failed = [c for c in CHECKS if not c[4]]
    if failed:
        print(f"\n{len(failed)} check(s) FAILED; site/assets/adv NOT updated.")
        return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    for stem in CHARTS:
        (ASSETS / f"{stem}.svg").write_text(fragments[stem], encoding="utf-8", newline="\n")
    shutil.copyfile(BUILD / "new_registrants.csv", ASSETS / "new_registrants.csv")
    print(f"\nall {len(CHECKS)} checks passed; wrote")
    for p in sorted(ASSETS.iterdir()):
        print(f"  {p.relative_to(REPO)}  {p.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
