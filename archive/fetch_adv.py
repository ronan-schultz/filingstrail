#!/usr/bin/env python3
"""
Archive every Form ADV monthly extract the SEC currently hosts.

The SEC's index lists only the last fifteen months or so of these files. Older
vintages are delisted, not (as of 2026-09-10) deleted: they still resolve at
their original URLs, which the Wayback Machine's December 2024 snapshot of the
index preserves back to 2006. Nothing guarantees they stay. A panel needs
consecutive vintages, so this keeps a local, checksummed copy of every file,
and --extra-urls backfills delisted ones.

Behaviour:
  - Scrapes the SEC index for every ia<MMDDYYYY>*.xlsx / .zip link, registered
    and exempt variants both.
  - HEADs each one; downloads only if it is new or its size/Last-Modified moved.
  - Never overwrites. The SEC re-uploads under suffixed names (_0, _1) and may
    re-upload under the same name; a same-name file with different bytes is
    stored alongside under a sha-suffixed name and both are recorded.
  - Classifies each file by CONTENT (column count, Firm Type), not by filename.
    A bare 2026 .zip once served exempt-adviser data at a registered-looking
    path. Name/content disagreements are logged as warnings.
  - Records every archived file in MANIFEST.csv (append-only) and every file
    that disappears from the index in PRUNED.csv, with the date it was first
    seen missing.

SEC fair-access policy requires a User-Agent carrying contact details. Set
SEC_USER_AGENT or put it in archive/.useragent (gitignored).

    python archive/fetch_adv.py --dry-run
    python archive/fetch_adv.py
    pythonw archive/fetch_adv.py --log archive/fetch.log     # scheduled, no console
    python archive/fetch_adv.py --extra-urls archive/backfill-2023-2024.txt
"""

import argparse
import csv
import hashlib
import io
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

INDEX = ("https://www.sec.gov/data-research/sec-markets-data/"
         "information-about-registered-investment-advisers-exempt-reporting-advisers")
HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
MANIFEST = HERE / "MANIFEST.csv"
PRUNED = HERE / "PRUNED.csv"
UA_FILE = HERE / ".useragent"

MANIFEST_FIELDS = ["fetched_utc", "source", "filename", "stored_as", "vintage", "data_through",
                   "variant_by_name", "variant_by_content", "firm_type", "n_cols",
                   "n_rows", "bytes", "sha256", "last_modified", "etag", "url"]
DATE_COL = "Latest ADV Filing Date"
PRUNED_FIELDS = ["first_missing_utc", "filename", "vintage"]

log = logging.getLogger("fetch_adv")


# ---------------------------------------------------------------- http

def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua and UA_FILE.exists():
        ua = UA_FILE.read_text(encoding="utf8").strip()
    if "@" not in ua:
        raise SystemExit("SEC fair-access policy requires a User-Agent with a contact "
                         "email. Set SEC_USER_AGENT or write archive/.useragent.")
    return ua


def open_url(url: str, ua: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=180)


def list_hosted(ua: str) -> dict:
    html = open_url(INDEX, ua).read().decode("utf8", "replace")
    found = {}
    for href, name in re.findall(r'href="([^"]*/(ia\d{8}[^"/]*\.(?:xlsx|zip)))"', html, re.I):
        found[name] = href if href.startswith("http") else "https://www.sec.gov" + href
    # Zero links means the page changed shape, not that the SEC stopped
    # publishing. Fail loudly rather than record a clean run that archived nothing.
    if not found:
        raise SystemExit("No ia*.xlsx/.zip links on the SEC index. Page layout changed?")
    return found


# ---------------------------------------------------------------- content

def vintage(name: str) -> str:
    """ia09012026 (2025+) or ia090324 (2024 and earlier) -> YYYY-MM-DD."""
    m = re.search(r"ia(\d{2})(\d{2})(\d{4})(?!\d)", name)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    m = re.search(r"ia(\d{2})(\d{2})(\d{2})(?!\d)", name)
    return f"20{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


def variant_by_name(name: str) -> str:
    n = name.lower()
    if "-exempt" in n:
        return "exempt"
    if "-registered" in n:
        return "registered"
    return "unmarked"


def _iso(v) -> str:
    """'07/31/2025', a datetime, or '2025-07-31 00:00:00' -> '2025-07-31'."""
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def _xlsx_head(fileobj):
    import openpyxl
    wb = openpyxl.load_workbook(fileobj, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(min_row=1, max_row=2, values_only=True)
    header = [c for c in next(rows)]
    while header and header[-1] is None:
        header.pop()
    header = [str(h) for h in header]
    first = list(next(rows, ()))
    n_rows = (ws.max_row or 1) - 1          # from the sheet's dimension tag
    through = ""
    if DATE_COL in header:
        c = header.index(DATE_COL) + 1
        for (v,) in ws.iter_rows(min_row=2, min_col=c, max_col=c, values_only=True):
            through = max(through, _iso(v))
    wb.close()
    return header, first, n_rows, through


def inspect(path: Path):
    """(header, first data row, data row count, data-through date) without a full load.

    data_through is the latest ADV filing date anywhere in the file: the date the
    roster actually reflects. The filename date is when the SEC posted it, and the
    two can disagree -- ia09022025.xlsx is the July 31, 2025 roster republished.
    """
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.namelist()
                       if m.lower().endswith((".csv", ".xlsx")) and not m.startswith("__MACOSX")]
            m = max(members, key=lambda x: z.getinfo(x).file_size)
            if m.lower().endswith(".xlsx"):
                return _xlsx_head(io.BytesIO(z.read(m)))
            with z.open(m) as f:
                r = csv.reader(io.TextIOWrapper(f, encoding="utf8", errors="replace",
                                                newline=""))
                header = next(r)
                i = header.index(DATE_COL) if DATE_COL in header else None
                first, n, through = [], 0, ""
                for row in r:
                    if not first:
                        first = row
                    n += 1
                    if i is not None and i < len(row):
                        through = max(through, _iso(row[i]))
                return header, first, n, through
    return _xlsx_head(str(path))


def classify(header, first):
    firm_type = ""
    if "Firm Type" in header:
        i = header.index("Firm Type")
        firm_type = str(first[i]).strip() if i < len(first) and first[i] is not None else ""
    ft = firm_type.lower()
    if ft == "registered":
        return "registered", firm_type
    if ft == "era" or "exempt" in ft:          # the exempt extracts say 'ERA'
        return "exempt", firm_type
    return "unknown", firm_type


def stale_against(manifest: list, name: str, variant: str, through: str):
    """The previous vintage of the same variant, if this one doesn't advance past it."""
    v = vintage(name)
    earlier = [r for r in manifest
               if r["variant_by_content"] == variant and r["vintage"] < v
               and r.get("data_through")]
    if not earlier or not through:
        return None
    prev = max(earlier, key=lambda r: r["vintage"])
    return prev if through <= prev["data_through"] else None


# ---------------------------------------------------------------- records

def read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf8") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, fields: list, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- main

def read_extra(path) -> dict:
    """URLs to archive beyond the live index, one per line, '#' comments allowed."""
    out = {}
    for line in Path(path).read_text(encoding="utf8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out[line.rsplit("/", 1)[-1]] = line
    return out


def run(dry_run: bool, extra_urls=None) -> int:
    ua = user_agent()
    RAW.mkdir(exist_ok=True)
    manifest = read_csv(MANIFEST)
    latest = {}
    for row in manifest:                      # last entry per filename wins
        latest[row["filename"]] = row
    known_shas = {row["sha256"] for row in manifest}

    hosted = list_hosted(ua)
    log.info("SEC index lists %d files", len(hosted))
    targets = {n: (u, "index") for n, u in hosted.items()}
    if extra_urls:
        extra = read_extra(extra_urls)
        for n, u in extra.items():
            targets.setdefault(n, (u, "backfill"))
        log.info("plus %d backfill URLs from %s", len(extra), extra_urls)

    errors = 0
    counts = {"new": 0, "revised": 0, "unchanged": 0}
    for name, (url, source) in sorted(targets.items()):
        try:
            with open_url(url, ua, "HEAD") as r:
                length = r.headers.get("Content-Length", "")
                lm = r.headers.get("Last-Modified", "")
                etag = r.headers.get("ETag", "")
            prev = latest.get(name)
            if prev and prev["bytes"] == length and prev["last_modified"] == lm:
                counts["unchanged"] += 1
                log.debug("unchanged  %s", name)
                continue
            status = "REVISED" if prev else "NEW"
            if dry_run:
                log.info("would fetch [%s] %s (%s bytes)", status, name, length)
                continue

            part = RAW / f".{name}.part"
            with open_url(url, ua) as r, part.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            with part.open("rb") as f:
                if f.read(4) != b"PK\x03\x04":        # xlsx and zip are both zip containers
                    part.unlink()
                    raise ValueError("response is not a zip/xlsx container (error page?)")

            digest = sha256(part)
            if digest in known_shas:
                # Same bytes we already hold; only the headers moved.
                part.unlink()
                counts["unchanged"] += 1
                log.info("same bytes, headers changed  %s", name)
                continue

            stored = name if not (RAW / name).exists() else \
                f"{Path(name).stem}.{digest[:12]}{Path(name).suffix}"
            final = RAW / stored
            part.rename(final)

            header, first, n_rows, through = inspect(final)
            by_content, firm_type = classify(header, first)
            by_name = variant_by_name(name)
            if by_name != "unmarked" and by_name != by_content:
                log.warning("NAME/CONTENT MISMATCH %s: name says %s, Firm Type says %r",
                            name, by_name, firm_type)
            prev_v = stale_against(manifest, name, by_content, through)
            if prev_v:
                log.warning("STALE VINTAGE %s: data only through %s, no later than %s "
                            "(through %s). Do not treat it as a new month.",
                            name, through, prev_v["filename"], prev_v["data_through"])

            row = {
                "fetched_utc": now(), "source": source, "filename": name, "stored_as": stored,
                "vintage": vintage(name), "data_through": through,
                "variant_by_name": by_name,
                "variant_by_content": by_content, "firm_type": firm_type,
                "n_cols": len(header), "n_rows": n_rows, "bytes": final.stat().st_size,
                "sha256": digest, "last_modified": lm, "etag": etag, "url": url,
            }
            append_csv(MANIFEST, MANIFEST_FIELDS, row)
            manifest.append(row)
            known_shas.add(digest)
            counts["new" if status == "NEW" else "revised"] += 1
            log.info("%-8s %s -> %s  [%s, %d cols, %s rows, data through %s]  sha256 %s",
                     status, name, stored, by_content, len(header), f"{n_rows:,}",
                     through or "?", digest[:16])
            time.sleep(1)                     # well inside SEC's 10 req/s limit
        except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile) as e:
            errors += 1
            log.error("FAILED %s: %s", name, e)

    # Files we hold that the SEC no longer lists. This is the evidence for the
    # pruning claim, and the date each one vanished.
    # Backfilled files were delisted before we ever saw them on the index, so
    # they are not evidence of pruning and never get a first-missing date.
    already = {row["filename"] for row in read_csv(PRUNED)}
    from_index = {n for n, r in latest.items() if r.get("source", "index") == "index"}
    for name in sorted(from_index - set(hosted) - already):
        if not dry_run:
            append_csv(PRUNED, PRUNED_FIELDS,
                       {"first_missing_utc": now(), "filename": name, "vintage": vintage(name)})
        log.info("PRUNED by SEC: %s (archived copy kept)", name)

    for row in latest.values():
        if not (RAW / row["stored_as"]).exists():
            errors += 1
            log.error("archived file missing from disk: %s", row["stored_as"])

    log.info("done: %d new, %d revised, %d unchanged, %d errors",
             counts["new"], counts["revised"], counts["unchanged"], errors)
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="HEAD only, download nothing")
    ap.add_argument("--extra-urls", help="file of delisted URLs to archive as well")
    ap.add_argument("--log", help="also append to this file (use with pythonw)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    handlers = []
    if sys.stdout is not None:                # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    sys.exit(run(args.dry_run, args.extra_urls))


if __name__ == "__main__":
    main()
