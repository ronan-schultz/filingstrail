#!/usr/bin/env python3
"""
New RIA registrants from SEC Form ADV monthly extracts.

Takes two monthly *registered adviser* files (current and ~12 months prior),
diffs them on Organization CRD# to isolate firms that appear now and did not
appear then, cross-validates that diff against each firm's SEC status
effective date, and emits a CSV plus a chart.

Source index (lists every monthly vintage and both variants):
  https://www.sec.gov/data-research/sec-markets-data/information-about-registered-investment-advisers-exempt-reporting-advisers

Two variants are published each month and they are NOT interchangeable:
  ia<MMDDYYYY>-registered.zip   registered advisers (~17k rows, 448 cols, has Item 5)
  ia<MMDDYYYY>-exempt.zip       exempt reporting advisers (~6.6k rows, 171 cols, no Item 5)
2025-and-earlier vintages are bare .xlsx with no "-registered" suffix. A bare
2026 .zip may be a stale ERA duplicate. This script refuses to run on anything
whose Firm Type column is not entirely "Registered".

Usage:
    python adv_new_registrants.py --inspect-only --current data/ia09012026-registered.zip
    python adv_new_registrants.py --current data/ia09012026-registered.zip \
                                  --prior   data/ia09022025.xlsx

The SEC does not publish a stable schema for these extracts and column names
drift between vintages. Nothing here hardcodes a column name blindly: the
script resolves each field against the actual header row, prints what it
matched, and asserts the matched column's *values* look like what the field is
supposed to be. Read the mapping and the value checks before trusting any
number that comes out of this.

Requires: pandas, matplotlib, openpyxl
"""

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

# Candidate header patterns per logical field, tried in order. Extend these
# rather than editing the resolution logic.
FIELD_PATTERNS = {
    "crd": [r"^\s*organization\s*crd", r"\bcrd\s*(number|#|no)?\s*$", r"^crd$"],
    "name": [r"primary\s*business\s*name", r"^\s*legal\s*name", r"\bbusiness\s*name\b"],
    "aum": [r"^5F\(2\)\(c\)$", r"5F\(?2\)?\(?c\)?",
            r"regulatory\s*assets?\s*under\s*management"],
    "employees": [r"^5A$", r"number\s*of\s*employees"],
    # 1I is the Y/N "do you have a website" flag, NOT a URL. The count column is
    # an independent cross-check; both are resolved and reconciled below.
    "web_flag": [r"^1I$"],
    "web_count": [r"total\s*number\s*of\s*website\s*addresses"],
    "state": [r"^main\s*office\s*state$", r"main\s*office.*state"],
    "city": [r"^main\s*office\s*city$", r"main\s*office.*city"],
    "firm_type": [r"^firm\s*type$"],
    "status": [r"^sec\s*current\s*status$"],
    "status_date": [r"^sec\s*status\s*effective\s*date$"],
    "filing_date": [r"^latest\s*adv\s*filing\s*date$"],
}

REQUIRED = ("crd", "name", "firm_type")

AUM_BANDS = [
    (0, 25_000_000, "<$25M"),
    (25_000_000, 100_000_000, "$25-100M"),
    (100_000_000, 500_000_000, "$100-500M"),
    (500_000_000, 1_000_000_000, "$500M-1B"),
    (1_000_000_000, float("inf"), ">$1B"),
]

BAND_ORDER = ["$0 reported"] + [lbl for _, _, lbl in AUM_BANDS] + ["unreported"]


# ---------------------------------------------------------------- loading

def load_path(path: str) -> pd.DataFrame:
    """Load a locally downloaded monthly file. Handles .zip, .xlsx and .csv."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"No such file: {p}")
    print(f"  reading {p.name} ({p.stat().st_size:,} bytes)")
    if p.suffix.lower() == ".zip":
        return _from_zip(p.read_bytes())
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p, dtype=str, low_memory=False, encoding_errors="replace")
    return pd.read_excel(p, dtype=str)


def _from_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members = [n for n in z.namelist()
                   if n.lower().endswith((".xlsx", ".xls", ".csv"))
                   and not n.startswith("__MACOSX")]
        if not members:
            raise SystemExit(f"No data file inside the zip: {z.namelist()}")
        # Largest member is the firm-level extract; the others are schedules.
        member = max(members, key=lambda n: z.getinfo(n).file_size)
        print(f"  reading {member}")
        data = z.read(member)
    buf = io.BytesIO(data)
    if member.lower().endswith(".csv"):
        return pd.read_csv(buf, dtype=str, low_memory=False, encoding_errors="replace")
    return pd.read_excel(buf, dtype=str)


# ---------------------------------------------------------------- mapping

def resolve(columns, field: str):
    for pattern in FIELD_PATTERNS[field]:
        rx = re.compile(pattern, re.I)
        for col in columns:
            if rx.search(str(col)):
                return col
    return None


def build_mapping(df: pd.DataFrame, label: str) -> dict:
    mapping = {f: resolve(df.columns, f) for f in FIELD_PATTERNS}
    print(f"\n  field mapping [{label}]:")
    for field, col in mapping.items():
        print(f"    {'ok ' if col else '-- '}{field:<12} -> {col}")
    missing = [f for f in REQUIRED if not mapping[f]]
    if missing:
        raise SystemExit(
            f"\nRequired field(s) unresolved in {label}: {missing}. "
            "Add the real header to FIELD_PATTERNS and rerun."
        )
    return mapping


def check_registered(df: pd.DataFrame, m: dict, label: str) -> None:
    """Refuse to proceed on an exempt-reporting-adviser file."""
    kinds = set(df[m["firm_type"]].dropna().astype(str).str.strip().str.lower())
    if kinds != {"registered"}:
        raise SystemExit(
            f"\n{label} is not a pure registered-adviser extract "
            f"(Firm Type = {sorted(kinds)}). Download the '-registered' variant."
        )
    print(f"  firm type check [{label}]: all {len(df):,} rows Registered")


def check_values(df: pd.DataFrame, m: dict, label: str) -> None:
    """Assert each matched column's values look like the field it claims to be.

    A column name matching a pattern is not evidence the column holds what you
    think it holds. Item 1.I in particular is a Y/N flag, not a URL.
    """
    print(f"\n  value checks [{label}]:")

    crd = norm_crd(df[m["crd"]])
    assert crd.str.fullmatch(r"\d+").mean() > 0.99, f"{label}: CRD column is not numeric ids"
    print(f"    crd          all-numeric, {crd.nunique():,} unique, "
          f"{crd.duplicated().sum()} duplicate rows")

    if m["web_flag"]:
        vals = set(df[m["web_flag"]].dropna().astype(str).str.strip().str.upper())
        assert vals <= {"Y", "N"}, f"{label}: web_flag not Y/N, got {sorted(vals)[:6]}"
        n_no = int((df[m["web_flag"]].astype(str).str.strip().str.upper() == "N").sum())
        line = f"    web_flag     Y/N only; {n_no:,} rows = N"
        if m["web_count"]:
            zero = int((to_number(df[m["web_count"]]).fillna(-1) == 0).sum())
            agree = zero == n_no
            line += f"; {zero:,} rows report 0 websites -> "
            line += "agrees" if agree else "DISAGREES"
            assert agree, f"{label}: website flag and website count disagree"
        print(line)

    if m["employees"]:
        emp = to_number(df[m["employees"]])
        assert emp.notna().mean() > 0.95, f"{label}: employees column mostly unparseable"
        assert emp.min() >= 0, f"{label}: negative employee counts"
        print(f"    employees    median {emp.median():.0f}, "
              f"p90 {emp.quantile(.9):.0f}, max {emp.max():,.0f}")

    if m["aum"]:
        aum = to_number(df[m["aum"]])
        assert aum.notna().mean() > 0.95, f"{label}: AUM column mostly unparseable"
        print(f"    aum          median ${aum.median():,.0f}, "
              f"total ${aum.sum() / 1e12:,.1f}T, {int((aum == 0).sum()):,} report $0")


# ---------------------------------------------------------------- helpers

def norm_crd(series: pd.Series) -> pd.Series:
    """CRDs arrive as '123456', '123456.0' or ' 123456 ' depending on vintage."""
    return (series.astype(str).str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"[^0-9]", "", regex=True))


def to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True).replace("", None),
        errors="coerce",
    )


def band(v):
    if pd.isna(v):
        return "unreported"
    if v == 0:
        return "$0 reported"
    for lo, hi, label in AUM_BANDS:
        if lo <= v < hi:
            return label
    return "unreported"


def data_through(df: pd.DataFrame, m: dict):
    """Latest ADV filing date in the file: the date the roster actually reflects."""
    if not m.get("filing_date"):
        return None
    return pd.to_datetime(df[m["filing_date"]], errors="coerce", format="mixed").max()


def human(d) -> str:
    """Timestamp -> 'Aug 1, 2025'. strftime's %-d is not portable to Windows."""
    return f"{d:%b} {d.day}, {d.year}"


def stem_date(stem: str) -> str:
    """'ia09022025' -> '2025-09-02'."""
    mm = re.search(r"ia(\d{2})(\d{2})(\d{4})", stem)
    return f"{mm.group(3)}-{mm.group(1)}-{mm.group(2)}" if mm else "1900-01-01"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", required=True)
    ap.add_argument("--prior")
    ap.add_argument("--inspect-only", action="store_true")
    ap.add_argument("--out", default="new_registrants")
    args = ap.parse_args()

    cur_label = Path(args.current).stem
    print(f"current: {args.current}")
    cur = load_path(args.current)
    print(f"  {len(cur):,} rows, {len(cur.columns)} columns")
    m = build_mapping(cur, cur_label)
    check_registered(cur, m, cur_label)
    check_values(cur, m, cur_label)

    if args.inspect_only:
        print("\n  all columns:")
        for c in cur.columns:
            print(f"    {c}")
        return

    if not args.prior:
        raise SystemExit("--prior is required unless --inspect-only")

    pri_label = Path(args.prior).stem
    print(f"\nprior: {args.prior}")
    pri = load_path(args.prior)
    print(f"  {len(pri):,} rows, {len(pri.columns)} columns")
    pm = build_mapping(pri, pri_label)
    check_registered(pri, pm, pri_label)
    check_values(pri, pm, pri_label)

    # ---- the diff
    cur = cur.assign(_crd=norm_crd(cur[m["crd"]]))
    pri_crd = set(norm_crd(pri[pm["crd"]]))
    new = cur[~cur["_crd"].isin(pri_crd)].copy()

    net = len(cur) - len(pri)
    left = len(pri_crd - set(cur["_crd"]))
    print("\n--- diff ---")
    print(f"{pri_label}: {len(pri):,} registered")
    print(f"{cur_label}: {len(cur):,} registered   (net {net:+,})")
    print(f"entered (in current, not in prior): {len(new):,}")
    print(f"exited  (in prior, not in current): {left:,}")
    assert len(new) - left == net, "entered minus exited must equal the net change"
    if len(new) == 0:
        raise SystemExit("Zero new registrants -- are these the same vintage?")

    # ---- what dates the two rosters actually reflect
    # The filename date is when the SEC posted the file, not what's in it.
    # ia09022025.xlsx is the July 31, 2025 roster republished with one extra
    # column; keying the window on its filename overstates it by a month and
    # misclassifies August 2025 registrations as re-registrations.
    pri_thru, cur_thru = data_through(pri, pm), data_through(cur, m)
    print("\n--- window ---")
    for lbl, thru in ((pri_label, pri_thru), (cur_label, cur_thru)):
        lag = (pd.Timestamp(stem_date(lbl)) - thru).days if thru is not None else None
        note = f"   <-- {lag} days behind its filename: stale republish" if lag and lag > 7 else ""
        print(f"{lbl}: data through {thru:%Y-%m-%d}{note}")
    months = (cur_thru.year - pri_thru.year) * 12 + (cur_thru.month - pri_thru.month)
    print(f"window: {pri_thru + pd.Timedelta(days=1):%Y-%m-%d} to {cur_thru:%Y-%m-%d} "
          f"({months} months of filings)")

    # ---- cross-validate the diff against SEC status effective date
    if m["status_date"]:
        eff = pd.to_datetime(new[m["status_date"]], format="%m/%d/%Y", errors="coerce")
        cutoff = pri_thru.normalize() + pd.Timedelta(days=1)
        within = int((eff >= cutoff).sum())
        print("\n--- diff validation ---")
        print(f"SEC status effective date on/after {cutoff:%Y-%m-%d}: "
              f"{within:,} of {len(new):,} ({within / len(new) * 100:.1f}%)")
        print(f"  older effective date (re-registration / status change): "
              f"{int((eff < cutoff).sum()):,}")
        print(f"  unparseable date: {int(eff.isna().sum()):,}")
        new["_eff_date"] = eff
        new["_new_registration"] = eff >= cutoff

    # ---- derived cuts
    new["_aum"] = to_number(new[m["aum"]]) if m["aum"] else pd.NA
    new["_band"] = new["_aum"].map(band)
    if m["employees"]:
        new["_emp"] = to_number(new[m["employees"]])
    if m["web_flag"]:
        new["_site"] = new[m["web_flag"]].astype(str).str.strip().str.upper().eq("Y")

    keep = [m[f] for f in ("crd", "name", "aum", "employees", "web_flag", "web_count",
                           "state", "city", "status", "status_date", "filing_date") if m[f]]
    keep += [c for c in ("_aum", "_band", "_emp", "_site", "_new_registration")
             if c in new.columns]
    out_csv = f"{args.out}.csv"
    new[keep].to_csv(out_csv, index=False)
    print(f"\nwrote {out_csv}")

    # ---- headline stats: these are the numbers that go in the email
    print("\n--- headline ---")
    print(f"new registrants: {len(new):,}")
    if "_site" in new:
        n = int((~new["_site"]).sum())
        base = (~cur[m["web_flag"]].astype(str).str.strip().str.upper().eq("Y")).mean()
        print(f"no website: {n:,} ({n / len(new) * 100:.1f}%)   "
              f"[all registered advisers: {base * 100:.1f}%]")
    if "_emp" in new:
        for t in (1, 5, 10):
            k = int((new["_emp"] <= t).sum())
            print(f"{t} or fewer employees: {k:,} ({k / len(new) * 100:.1f}%)")
        print(f"median employees: {new['_emp'].median():.0f}")
    print("\nby AUM band:")
    tab = new["_band"].value_counts().reindex(BAND_ORDER).dropna().astype(int)
    for k, v in tab.items():
        print(f"  {k:<12} {v:>6,}  {v / len(new) * 100:>5.1f}%")
    print(f"\nmedian AUM: ${new['_aum'].median():,.0f}")
    if m["state"]:
        print("\ntop states:")
        print(new[m["state"]].value_counts().head(10).to_string())

    # ---- the chart that goes in the email
    if "_site" in new:
        baseline = float((~cur[m["web_flag"]].astype(str).str.strip()
                          .str.upper().eq("Y")).mean() * 100)
        chart(new, args.out, pri_thru, cur_thru, baseline)


def chart(new, out, pri_thru, cur_thru, baseline):
    """Share with no website, by AUM band, against the all-adviser baseline.

    The raw-count version buries the finding: the interesting number is the
    rate, and the rate is flat at ~13% across every band that reports real
    assets, then jumps for the firms reporting $0.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = [b for b in BAND_ORDER if b in set(new["_band"])]
    g = new.groupby("_band")["_site"]
    tab = pd.DataFrame({"n": g.size(), "no_site": g.apply(lambda s: (~s).sum())})
    tab = tab.reindex(order)
    tab["pct"] = tab["no_site"] / tab["n"] * 100

    fig, ax = plt.subplots(figsize=(8.5, 4.6), dpi=200)
    bars = ax.bar(range(len(tab)), tab["pct"], width=0.66, color="#7A8AA0")
    bars[0].set_color("#C05746")  # the $0-reported cohort is the outlier

    ax.axhline(baseline, ls="--", lw=1.2, color="#444", zorder=3)
    ax.text(0.5, baseline + 0.9, f"all registered advisers: {baseline:.1f}%",
            ha="left", va="bottom", fontsize=8.5, color="#444")

    for i, pct in enumerate(tab["pct"]):
        ax.text(i, pct + 0.9, f"{pct:.0f}%", ha="center", fontsize=10.5, weight="bold")

    ax.set_xticks(range(len(tab)))
    ax.set_xticklabels([f"{b}\nn={int(n):,}" for b, n in zip(tab.index, tab["n"])],
                       fontsize=9.5)
    ax.set_ylabel("share with no website")
    ax.set_ylim(0, max(tab["pct"]) * 1.22)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.set_title("New SEC-registered advisers are twice as likely to have no website\n"
                 f"{len(new):,} firms registered "
                 f"{human(pri_thru + pd.Timedelta(days=1))} to {human(cur_thru)}, "
                 "by reported regulatory AUM",
                 loc="left", fontsize=11.5)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="x", length=0, pad=6)
    ax.text(0, -0.27, "Source: SEC Form ADV monthly registered-adviser extracts, "
                       "diffed on Organization CRD#. AUM = Item 5F(2)(c); "
                       "website = Item 1.I.",
            transform=ax.transAxes, fontsize=7.5, color="#666")
    fig.tight_layout()
    fig.savefig(f"{out}.png", bbox_inches="tight")
    print(f"wrote {out}.png")


if __name__ == "__main__":
    sys.exit(main())
