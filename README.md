# Filings Trail

Source for [filingstrail.com](https://filingstrail.com): panels built from public
SEC filing data, tracking the same firms across monthly vintages instead of
counting them at a point in time.

## Contents

- `adv-report/` — the Form ADV new-registrant panel, published at
  [filingstrail.com/adv](https://filingstrail.com/adv).
  `REPORT.md` is the text. `METHOD.md` carries sources, checksums, the field
  mapping and the limits. `report_stats.py` prints every figure in the report.
  `adv_new_registrants.py` does the CRD diff and writes the firm-level CSV.
- `archive/` — `fetch_adv.py` keeps a checksummed copy of every monthly Form ADV
  extract the SEC hosts; `MANIFEST.csv` records each file, including the date
  its data actually runs through.
- `site/` — the static site: a Markdown render, chart SVGs regenerated from the
  chart code in `adv-report/`, and the verification gate that has to pass
  before a deploy.

## Reproduce

The raw SEC extracts are not committed. `adv-report/METHOD.md` lists their URLs
and sha256 checksums. Download the two files it names into `adv-report/data/`,
then:

```
cd adv-report
python report_stats.py
```

Python 3.14 with pandas, matplotlib and openpyxl.
