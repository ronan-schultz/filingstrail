#!/usr/bin/env python3
"""Every number that appears in REPORT.md, printed by code.

The report cites no figure that is not produced here. Run:

    python report_stats.py

Depends on the same two extracts as adv_new_registrants.py; see METHOD.md for
URLs and checksums.
"""

import importlib.util
from pathlib import Path

import pandas as pd

CUR = "data/ia09012026-registered.zip"
PRI = "data/ia09022025.xlsx"

spec = importlib.util.spec_from_file_location("adv", Path(__file__).parent / "adv_new_registrants.py")
adv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adv)


def prep(df):
    df = df.copy()
    df["_crd"] = adv.norm_crd(df["Organization CRD#"])
    df["_aum"] = adv.to_number(df["5F(2)(c)"])
    df["_emp"] = adv.to_number(df["5A"])
    df["_site"] = df["1I"].astype(str).str.strip().str.upper().eq("Y")
    df["_band"] = df["_aum"].map(adv.band)
    return df


def pct(k, n):
    return f"{k:,} ({k / n * 100:.1f}%)"


def rule(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


cur, pri = prep(adv.load_path(CUR)), prep(adv.load_path(PRI))

# ------------------------------------------------------------------ population
rule("1. POPULATION AND DIFF")
pri_crd, cur_crd = set(pri["_crd"]), set(cur["_crd"])
new = cur[~cur["_crd"].isin(pri_crd)].copy()
exited = len(pri_crd - cur_crd)
print(f"prior file registered ........... {len(pri):,}")
print(f"current file registered ......... {len(cur):,}")
print(f"net change ....................... {len(cur) - len(pri):+,}")
print(f"entered .......................... {len(new):,}")
print(f"exited ........................... {exited:,}")
assert len(new) - exited == len(cur) - len(pri)
print("entered - exited == net .......... OK")

rule("2. BASELINE: ALL REGISTERED ADVISERS, 2026-09-01")
print(f"no website (Item 1.I = N) ........ {pct(int((~cur['_site']).sum()), len(cur))}")
print(f"median reported AUM .............. ${cur['_aum'].median():,.0f}")
print(f"median employees ................. {cur['_emp'].median():.0f}")

rule("3. THE 1,693 NEW REGISTRANTS")
print(f"no website ....................... {pct(int((~new['_site']).sum()), len(new))}")
print(f"<=5 employees .................... {pct(int((new['_emp'] <= 5).sum()), len(new))}")
print(f"median employees ................. {new['_emp'].median():.0f}")
print(f"median reported AUM .............. ${new['_aum'].median():,.0f}")
print(f"reporting $0 AUM ................. {pct(int((new['_aum'] == 0).sum()), len(new))}")

print("\nno-website rate by AUM band:")
for b in adv.BAND_ORDER:
    s = new[new["_band"] == b]
    if len(s):
        print(f"  {b:<12} n={len(s):>5,}   {(~s['_site']).mean() * 100:>5.1f}%   "
              f"({(len(s) / len(new) * 100):.1f}% of cohort)")

# Pooled remainder. The per-band rates cluster near 12.6%, which is NOT the
# rate on the pooled non-$0 firms -- that has to be recomputed, not carried
# over from the middle bands.
rest = new[new["_aum"] != 0]
print(f"\nexcluding the $0 band: {int((~rest['_site']).sum()):,} of {len(rest):,} "
      f"= {(~rest['_site']).mean() * 100:.1f}% no website")

rule("4. WINDOW AND DIFF VALIDATION")
# Key the window on what the files contain, not what they're named.
# ia09022025.xlsx is the July 31, 2025 roster republished; see METHOD.md.
pri_thru = pd.to_datetime(pri["Latest ADV Filing Date"], errors="coerce", format="mixed").max()
cur_thru = pd.to_datetime(cur["Latest ADV Filing Date"], errors="coerce", format="mixed").max()
months = (cur_thru.year - pri_thru.year) * 12 + (cur_thru.month - pri_thru.month)
print(f"prior data through ............... {pri_thru:%Y-%m-%d}")
print(f"current data through ............. {cur_thru:%Y-%m-%d}")
print(f"window ........................... {pri_thru + pd.Timedelta(days=1):%Y-%m-%d} "
      f"to {cur_thru:%Y-%m-%d}, {months} months")
eff = pd.to_datetime(new["SEC Status Effective Date"], format="%m/%d/%Y", errors="coerce")
cut = pri_thru.normalize() + pd.Timedelta(days=1)
genuine = new[eff >= cut]
print(f"effective date on/after {cut:%Y-%m-%d} .. {pct(len(genuine), len(new))}")
print(f"older effective date ............. {pct(int((eff < cut).sum()), len(new))}")
print(f"unparseable ...................... {int(eff.isna().sum()):,}")
print(f"no-website rate, genuine only .... {(~genuine['_site']).mean() * 100:.1f}%")
file_cut = pd.Timestamp("2025-09-02")          # what the prior file's NAME implies
misread = int(((eff >= cut) & (eff < file_cut)).sum())
print(f"cutting on the filename date instead would call {int((eff < file_cut).sum())} "
      f"re-registrations, {misread} of them effective {cut:%Y-%m-%d}..{file_cut - pd.Timedelta(days=1):%Y-%m-%d}")

# ------------------------------------------------------------------ the panel
rule(f"5. THE PANEL: $0-AUM COHORT AS OF {pri_thru:%Y-%m-%d}, TRACKED {months} MONTHS")
z = pri[pri["_aum"] == 0].copy()
m = cur.set_index("_crd")
f = m.reindex(z["_crd"])
z["_alive"] = f["_aum"].notna().values
z["_aum26"] = f["_aum"].values
z["_site26"] = f["_site"].values
z["_converted"] = (z["_aum26"] > 0).fillna(False)

dereg = int((~z["_alive"]).sum())
conv = int(z["_converted"].sum())
stuck = int((z["_alive"] & (z["_aum26"] == 0)).sum())
print(f"cohort size ($0 AUM at t0) ...... {len(z):,}")
print(f"  no website at t0 ............... {pct(int((~z['_site']).sum()), len(z))}")
print(f"deregistered by {cur_thru:%Y-%m-%d} ....... {pct(dereg, len(z))}")
print(f"still registered, now >$0 AUM .... {pct(conv, len(z))}")
print(f"still registered, still $0 ....... {pct(stuck, len(z))}")
assert dereg + conv + stuck == len(z)
print(f"never became a customer (dereg + still $0) ... "
      f"{pct(dereg + stuck, len(z))}")
print(f"conversion among survivors ....... {conv / (len(z) - dereg) * 100:.1f}%")

print("\nthe 154 with $0 AUM and no website at t0:")
n0 = z[~z["_site"]]
print(f"  deregistered ................... {int((~n0['_alive']).sum()):,}")
alive0 = n0[n0["_alive"]]
print(f"  still registered ............... {len(alive0):,}")
print(f"    acquired a website ........... {pct(int(alive0['_site26'].sum()), len(alive0))}")
print(f"    still no website ............. {pct(int((~alive0['_site26'].astype(bool)).sum()), len(alive0))}")

# ------------------------------------------------- what predicts conversion
rule(f"6. WHICH THIRD CONVERTS: t0 OBSERVABLES vs {months}-MONTH OUTCOME")


def split(mask, label):
    a, b = z[mask], z[~mask]
    for s, nm in ((a, label), (b, f"not {label}")):
        if len(s) < 15:
            continue
        print(f"  {nm:<28} n={len(s):>4}  converted {s['_converted'].mean() * 100:>5.1f}%  "
              f"dereg {(~s['_alive']).mean() * 100:>5.1f}%")


print("by website at t0:")
split(z["_site"], "had a website")
print("\nby employee count at t0:")
for lo, hi, nm in [(0, 2, "1 employee"), (2, 6, "2-5 employees"),
                   (6, 21, "6-20 employees"), (21, 1e9, "21+ employees")]:
    s = z[(z["_emp"] >= lo) & (z["_emp"] < hi)]
    if len(s) >= 15:
        print(f"  {nm:<28} n={len(s):>4}  converted {s['_converted'].mean() * 100:>5.1f}%  "
              f"dereg {(~s['_alive']).mean() * 100:>5.1f}%")

print("\njoint (the qualification screen):")
for nm, mask in [
    ("website AND >=2 employees", z["_site"] & (z["_emp"] >= 2)),
    ("website only", z["_site"] & (z["_emp"] < 2)),
    ("no website, >=2 employees", ~z["_site"] & (z["_emp"] >= 2)),
    ("no website AND 1 employee", ~z["_site"] & (z["_emp"] < 2)),
]:
    s = z[mask]
    if len(s) >= 15:
        print(f"  {nm:<28} n={len(s):>4}  converted {s['_converted'].mean() * 100:>5.1f}%  "
              f"dereg {(~s['_alive']).mean() * 100:>5.1f}%")

print("\nmedian 2026 AUM of the converters: "
      f"${z.loc[z['_converted'], '_aum26'].median():,.0f}")

# ------------------------------------------------------- is any of that real
rule("7. TWO-PROPORTION TESTS ON THE QUALIFICATION CLAIMS")


def ztest(a, b, la, lb, what):
    """Pooled two-proportion z-test. Small cells here; report n with every rate."""
    import math
    ka, na = int(a.sum()), len(a)
    kb, nb = int(b.sum()), len(b)
    pa, pb = ka / na, kb / nb
    p = (ka + kb) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    zs = (pa - pb) / se
    pv = math.erfc(abs(zs) / math.sqrt(2))
    print(f"  {what}")
    print(f"    {la:<26} {pa * 100:>5.1f}%  (n={na:,})")
    print(f"    {lb:<26} {pb * 100:>5.1f}%  (n={nb:,})")
    print(f"    difference {(pa - pb) * 100:+.1f}pp   z={zs:.2f}   p={pv:.4f}\n")


EMP_BANDS = [(0, 2, "1"), (2, 6, "2-5"), (6, 21, "6-20"), (21, 1e9, "21+")]


def headcount_chart(out="conversion_by_headcount.png"):
    """Conversion and deregistration by 2025 headcount.

    Only the single-signal bands are drawn. The crossed cells stay a table in
    the report: at n=40 in the smallest cell they are directional, and a chart
    implies a precision the counts do not support.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = []
    for lo, hi, nm in EMP_BANDS:
        s = z[(z["_emp"] >= lo) & (z["_emp"] < hi)]
        rows.append((nm, len(s), s["_converted"].mean() * 100,
                     (~s["_alive"]).mean() * 100))

    x = range(len(rows))
    w = 0.38
    fig, ax = plt.subplots(figsize=(8.5, 4.6), dpi=200)
    b1 = ax.bar([i - w / 2 for i in x], [r[2] for r in rows], w,
                label="reported assets thirteen months later", color="#7A8AA0")
    b2 = ax.bar([i + w / 2 for i in x], [r[3] for r in rows], w,
                label="deregistered", color="#C05746")
    b1[1].set_color("#3E5A78")  # the 2-5 band is the screen

    for bars in (b1, b2):
        for bar in bars:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.9,
                    f"{bar.get_height():.0f}%", ha="center", fontsize=9.5)

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{r[0]}\nn={r[1]:,}" for r in rows], fontsize=10)
    ax.set_xlabel(f"employees reported on Form ADV Item 5.A, {pri_thru:%B %Y} roster",
                  fontsize=9, labelpad=8)
    ax.set_ylabel("share of cohort")
    ax.set_ylim(0, 52)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.set_title("Headcount sorts the pre-launch cohort; website presence does not\n"
                 f"{len(z):,} SEC-registered advisers reporting $0 AUM as of "
                 f"{pri_thru:%b} {pri_thru.day}, {pri_thru.year}, tracked to "
                 f"{cur_thru:%b} {cur_thru.day}, {cur_thru.year}",
                 loc="left", fontsize=11.5)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="x", length=0)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    ax.text(0, -0.315,
            "Source: SEC Form ADV monthly registered-adviser extracts. Conversion gap "
            "between 1 and 2-5 employees: z=3.58, p=0.0003.\nThe 21+ band is not a "
            "failure rate: firms that large report $0 for structural reasons "
            "rather than because they are pre-launch, which its 4% "
            "deregistration rate shows.",
            transform=ax.transAxes, fontsize=7.5, color="#666", linespacing=1.6)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print(f"\nwrote {out}")


solo = z["_emp"] < 2
staffed = (z["_emp"] >= 2) & (z["_emp"] <= 5)
ztest(z.loc[staffed, "_converted"], z.loc[solo, "_converted"],
      "2-5 employees", "1 employee", "CONVERSION: headcount")
ztest(z.loc[solo, "_alive"].pipe(lambda s: ~s), z.loc[staffed, "_alive"].pipe(lambda s: ~s),
      "1 employee", "2-5 employees", "DEREGISTRATION: headcount")
ztest(z.loc[~z["_site"], "_converted"], z.loc[z["_site"], "_converted"],
      "no website at t0", "had a website at t0", "CONVERSION: website (the null result)")

headcount_chart()

# ------------------------------------------------ the stale-file evidence
rule("8. IS THE SEPTEMBER 2025 FILE A NEW MONTH? (needs archive/raw)")
ARCH = Path(__file__).parent.parent / "archive" / "raw"
pairs = [("ia07012025.xlsx", "ia08012025.xlsx"), ("ia08012025.xlsx", "ia09022025.xlsx"),
         ("ia07012026.zip", "ia08032026_1.zip"), ("ia08032026_1.zip", "ia09012026-registered.zip")]
if all((ARCH / f).exists() for pr in pairs for f in pr):
    crds = {}
    for f in {f for pr in pairs for f in pr}:
        crds[f] = set(adv.norm_crd(adv.load_path(str(ARCH / f))["Organization CRD#"]))
    for a_, b_ in pairs:
        print(f"  {a_:>26} -> {b_:<26} +{len(crds[b_] - crds[a_]):,} / -{len(crds[a_] - crds[b_]):,}")
else:
    print("  archive/raw not populated; run archive/fetch_adv.py")
