# Method — new SEC-registered investment advisers, Aug 2025 → Aug 2026

Run: `python adv_new_registrants.py --current data/ia09012026-registered.zip --prior data/ia09022025.xlsx`

## Sources

Both files come from the SEC's monthly Form ADV extract index:
<https://www.sec.gov/data-research/sec-markets-data/information-about-registered-investment-advisers-exempt-reporting-advisers>

| Role | File | sha256 | Rows | Cols | Data through |
|---|---|---|---|---|---|
| Current | [`ia09012026-registered.zip`](https://www.sec.gov/files/investment/data/other/information-about-registered-investment-advisers-exempt-reporting-advisers/ia09012026-registered.zip) | `2302e3ea00819b40f9f595dca53925137e9a6efacbdbfb8d912e66780c92601e` | 17,149 | 448 | 2026-08-31 |
| Prior | [`ia09022025.xlsx`](https://www.sec.gov/files/investment/data/information-about-registered-investment-advisers-exempt-reporting-advisers/ia09022025.xlsx) | `977d83fdd67acdc464d30d892fe1a839fae2d8d6a47661ecb7ec53bd0e7c312c` | 16,359 | 448 | 2025-07-31 |
| Look-back | [`ia070124.zip`](https://www.sec.gov/files/investment/data/information-about-registered-investment-advisers-exempt-reporting-advisers/ia070124.zip) | `790f991509046dacf7943906c1f04237ca6386de679b8d93235206b20af2a1b4` | 15,754 | 461 | 2024-06-30 |

Current and prior downloaded 2026-09-07 with a `User-Agent` identifying the requester, per
SEC policy; both checksums re-verified 2026-09-10 against fresh downloads by
`archive/fetch_adv.py`, which fetched the look-back file the same day. The look-back file
decides which $0-AUM firms on the prior roster are new registrants, for the panel.
"Data through" is the latest ADV filing date inside each file, which is not the date
in its name. See the date trap below.

### The file-selection trap

The SEC publishes **two** extracts per month and they are not interchangeable:

- `ia<MMDDYYYY>-registered.zip` — registered investment advisers. ~17k rows, 448
  columns, includes Item 5 (AUM, employees, clients).
- `ia<MMDDYYYY>-exempt.zip` — exempt reporting advisers. ~6.6k rows, 171 columns,
  **no Item 5 at all** (ERAs file a truncated ADV: Items 1, 2B, 3, 6, 7, 10, 11, 12).

2025-and-earlier vintages are bare `.xlsx` with no `-registered` suffix, so the
naming convention changed mid-series. A bare 2026 `.zip` still resolves at the old
path but serves ERA content — the first download attempt for this analysis pulled
`ia08032026.zip` and got 6,604 ERA rows with no AUM column. The script now refuses
to run unless `Firm Type` is entirely `Registered` in both files.

### The date trap

The filename date is when the SEC posted a file, not what the file contains.
`ia09022025.xlsx` is the July 31, 2025 roster republished under a September name:

- It holds exactly the same 16,359 CRDs as `ia08012025.xlsx`. Real months churn: the
  adjacent months in the archive run +182/−68, +159/−76 and +207/−76 firms. August to
  September 2025 is +0/−0.
- Its latest ADV filing date is 2025-07-31, identical to the August file.
- The only difference is one added column, `SEC Current Status`.

The window is therefore August 1, 2025 to August 31, 2026, thirteen months. Cutting the
validation on the filename date instead (2025-09-02) misclassifies every August 2025
registration as a re-registration. An earlier version of this analysis did that and
reported 1,573 validated firms (92.9%) and 120 re-registrations; 116 of those 120 have
SEC effective dates in August 2025. The code now derives both window ends from file
contents, and `archive/fetch_adv.py` flags any vintage whose data does not advance past
the previous one.

## Field mapping

Resolved against the real header row and asserted on values, not hardcoded:

| Logical field | Column | Verification |
|---|---|---|
| CRD | `Organization CRD#` | 100% numeric, 17,149 unique, 0 duplicates |
| Name | `Primary Business Name` | — |
| AUM | `5F(2)(c)` | >95% parseable; median $419M, total $177.0T |
| Employees | `5A` | >95% parseable, non-negative; median 8, p90 63 |
| Website | `1I` | **Y/N flag, not a URL.** Values ⊆ {Y, N} |
| Website count | `Total Number of Website Addresses` | 1,390 zeros == 1,390 `1I`=N — independent agreement |
| State / City | `Main Office State` / `Main Office City` | — |
| Status date | `SEC Status Effective Date` | 100% parses as `%m/%d/%Y` |

**Item 1.I is the trap.** It reads like a website address field and is a yes/no
flag. Treating it as a URL string makes every `N` count as *having* a website and
inverts the headline stat. The `Total Number of Website Addresses` reconciliation
is what proves the reading is right.

## Diff and its validation

Firms present in the current file and absent from the prior one, keyed on
normalized CRD (strips whitespace, trailing `.0`, non-digits).

```
prior   (data through 2025-07-31):  16,359 registered
current (data through 2026-08-31):  17,149 registered      net  +790
entered:      1,693
exited:         903                 1,693 − 903 = +790  ✓ (asserted)
```

Cross-check against each firm's own `SEC Status Effective Date`:

- **1,689 of 1,693 (99.8%)** have an effective date on or after 2025-08-01, the day after
  the prior file's data ends. These are genuine new registrations.
- 4 (0.2%) carry an older effective date. These are re-registrations, status changes,
  or firms that left and returned within the window.
- 0 unparseable.

The CSV flags these as `_new_registration` so any cut can be run either way. The
headline figures below use all 1,693; the no-website result is unchanged on the
1,689 subset (15.9%), so it is not an artifact of the re-registrations.

## Results

| Metric | Value |
|---|---|
| New registrants | **1,693** |
| No website (Item 1.I = N) | **269 (15.9%)** vs **8.1%** for all 17,149 registered advisers |
| ≤5 employees | 1,181 (69.8%) — median headcount 4 |
| ≤1 employee | 249 (14.7%) |
| Median reported AUM | $120.1M (vs $419.4M for all registered advisers) |
| Reporting $0 AUM | 309 (18.3%) |

No-website rate by AUM band:

| Band | n | no website |
|---|---|---|
| $0 reported | 309 | 32.4% |
| <$25M | 54 | 13.0% |
| $25–100M | 111 | 12.6% |
| $100–500M | 1,046 | 12.6% |
| $500M–1B | 83 | 7.2% |
| >$1B | 90 | 11.1% |

Top states: NY 199, CA 171, FL 135, TX 93, IL 63, MA 50, NJ 39, OH 38, PA 38, CT 38.

## Limits — state these, don't hide them

1. **The $100–500M concentration is mechanical, not a finding.** $100M in regulatory
   AUM is the threshold at which an adviser must move from state to SEC registration.
   61.8% of new SEC registrants landing in that band is the threshold doing its job.
   Anyone in this market knows that; presenting it as a discovery is disqualifying.
2. **"New to the SEC" ≠ "new firm."** A large share of these are existing
   state-registered advisers crossing $100M, not startups. The extract cannot
   distinguish the two. The 4 firms (0.2%) with older effective dates are the only
   part of this the file makes visible; the rest is not measurable from it.
3. **The no-website result is concentrated in the $0-AUM cohort.** Excluding those
   309 firms, the rate is 12.2% (169 of 1,384) — still above the 8.1% baseline, but
   the "twice as likely" headline leans on the $0 group. Say so. Note that 12.2% is
   the pooled remainder and has to be recomputed; it is not the ~12.6% that the
   middle bands individually show.
4. **$0 reported AUM is ambiguous.** It can mean a newly approved adviser that has
   not yet taken on assets, an adviser with only non-discretionary or non-securities
   assets, or a reporting gap. Not investigated here.
5. **Point-in-time, not a flow.** This is a two-snapshot diff. Firms that registered
   and deregistered between the two dates are invisible.
6. Umbrella registrations mean one CRD can cover multiple relying advisers, so the
   firm count is not a count of legal entities.
7. **The panel counts new registrants only.** Of the 628 firms reporting $0 on the
   July 31, 2025 roster, the 288 absent from the June 30, 2024 roster are the panel; the
   340 already registered are a different population (they converted at 6.8%) and are
   never pooled in. "Deregistered" means absent from the August 31, 2026 registered
   roster: closing, merging and moving to state registration look the same.
