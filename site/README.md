# filingstrail.com

Static site for Filings Trail. `dist/` is the deploy root and is served by Vercel
as-is, with no build step on Vercel's side. Everything in `dist/` is generated;
never edit it by hand.

```
site.json          site config: nav, footer, works (published and in progress),
                   the method page's panels, the data page's manifest rules
content/           authored copy, rendered verbatim: method-intro.md, data-intro.md
charts.py          re-runs the frozen chart code in ../adv-report, writes assets/<slug>/
assets/<slug>/     chart SVG fragments and the firm-level CSV (charts.py output)
build.py           Markdown + manifest -> dist/ (deletes and regenerates dist/, keeps dist/.vercel)
pdf.py             prints dist/<slug>/index.html to dist/<slug>/<pdf> with headless Edge
verify.py          the deploy gate; exit 1 on any failure
checks/            expected figures, forbidden strings and page contracts for verify.py
templates/         page shell, root page, work page, doc page (method, data), 404
static/            style.css and favicon.svg, both inlined into every page
build/             charts.py scratch output (not deployed)
```

## Pages

| URL | source |
|---|---|
| `/` | `templates/root.html` (copy) + the `works` list from `site.json` |
| `/<slug>` | one per published work: its `REPORT.md`, rendered, plus appendix blocks |
| `/method` | `content/method-intro.md` + one section per entry in `method.panels` |
| `/data` | `content/data-intro.md` + `../archive/MANIFEST.csv` as two tables |
| `/data/manifest.csv` | `../archive/MANIFEST.csv`, copied byte for byte |
| `404.html` | `templates/404.html` |

Every page, the root included, carries the same masthead: the wordmark and the three
`nav` links from `site.json`. On the root page the wordmark is wrapped in the page's
`<h1>`; elsewhere it is a plain link with the same styling. Every page but the root
ends with the same one-line footer (site name, tagline, "Code on GitHub" to
`repo_url`). The root page's footer is the only place the author's name appears, and
`build.py` fails if the name turns up anywhere else. The current page's nav link gets
`aria-current="page"`; on a work page the "Work" link gets `aria-current="true"`.
Masthead and footer are hidden in print, so the PDF is the article alone.

## Rebuild

From `site/`, with the packages in `requirements.txt` and Edge or Chrome installed:

```
python charts.py
python build.py
python pdf.py
python verify.py
```

The order matters: `build.py` deletes the PDF along with everything else in
`dist/`, and `pdf.py` prints from the built page. Output is byte-for-byte
deterministic except the PDF, so a rebuild with unchanged inputs changes nothing.

`verify.py` fails while `repo_url` in `site.json` is null. With `repo_url` set,
the work page links "Repository" (the repository root) before its first heading,
code spans that name a git-tracked file link to `<repo_url>/blob/<repo_branch>/<path>`,
and every non-root footer links the repository. A trailing slash or `.git` on
`repo_url` is stripped. `python verify.py --allow-missing-repo` downgrades that one
check to a warning for local work; never deploy a build that needed it.

`build.py` refuses to guess. It exits 1 if a Markdown image has no SVG mapped in
`site.json`, if the report's `# ` title differs from the `site.json` title, if an
appendix's start/end markers, a dropped sentence or a renamed heading is not found
exactly once, if a phrase link's text is not found exactly once in its section, if
a manifest row has a variant or source it does not know, if the stale-file set
differs from `data.expected_stale`, if a GitHub link in authored copy points outside
`repo_url` or at an untracked file, if a root-absolute link or `#fragment` does not
resolve, if a page has duplicate ids, unbalanced tags, or any script other than the
analytics tag, or if a page contains one of a work's forbidden strings (read from
its `checks` file). Each of those means a source file changed and a page needs a look.

## The gate

`verify.py` does not trust `build.py`: it parses what is in `dist/` and compares it
with the sources (`../adv-report/REPORT.md`, `../adv-report/METHOD.md`,
`../archive/MANIFEST.csv`, `content/*.md`) and with its own expectations in `checks/`:

| file | holds |
|---|---|
| `checks/site.json` | site-wide values: name, author, email, the pinned `repo_url` and branch, page-weight limit, viewports |
| `checks/adv.json` | the report's gate figures, chart expectations, CSV reconciliation, forbidden strings |
| `checks/pages.json` | masthead, footers, root work list, sitemap, a pinned copy of `content/method-intro.md` and `content/data-intro.md`, the /method extraction rules, the /data columns, panel files and `expected_stale` |

`checks/pages.json` duplicates some of `site.json` on purpose, so a copy edit, a
renamed heading or a changed stale set has to be made in two places and cannot slip
through by editing only the generator's config. The author's GitHub handle in
`repo_url` is exempt from the name-once check only inside `href` values that are the
pinned repository (or a path under it); anywhere else it counts as a stray name.

Options beyond the defaults: `--dist`, `--site-json`, `--checks` and `--out` point at
other copies (the self-test uses them); `--repo` is the repository root holding
`adv-report/` and `archive/` (default `..`); `--content` is the authored copy (default
`content/`); `--stats-log` is `report_stats.py` output for check M3 (default
`build/adv/report_stats.log`; without it M3 is skipped and does not block);
`--no-browser` skips the headless checks, so the run cannot pass; `--live URL` checks
production. `python checks/selftest.py` builds a known-good fixture under the system
temp directory, plants over a hundred defects one at a time and fails unless
`verify.py` catches every one.

## Deploy (operator)

```
cd site/dist
vercel deploy --prod
```

`dist/vercel.json` carries the project config: clean URLs (`/adv` serves
`adv/index.html`), no trailing slashes, a permanent redirect from
`www.filingstrail.com` to the apex, and two headers (`nosniff`,
`strict-origin-when-cross-origin`). Add both `filingstrail.com` and
`www.filingstrail.com` to the Vercel project. The first `vercel deploy` from
`dist/` creates `dist/.vercel/` (the project link); `build.py` leaves it in place.

Analytics is Vercel Web Analytics, first-party and cookieless: one
`<script defer src="/_vercel/insights/script.js">` per page. Enable Web Analytics
on the project, otherwise that path returns 404 and nothing is counted (the pages
are unaffected). After deploying, `python verify.py --live https://filingstrail.com`
checks what production actually serves.

## Add a piece of work

1. Produce its assets under `assets/<slug>/`: one SVG fragment per chart (same
   format and CSS-variable colour interface as the existing ones, ids prefixed per
   chart, root `<svg class="chart">` so labels get the page-colour halo) and any
   downloadable files.
2. Add one entry to `works` in `site.json`, in the order the root page should list
   it. A published entry has no `status` (or `"status": "published"`):

   | key | meaning |
   |---|---|
   | `slug` | URL segment: the page is `/<slug>`; not `method` or `data` |
   | `title` | must equal the report's `# ` title line; also the root-page link text |
   | `descriptor` | muted line under the title on the root page |
   | `description` | meta and Open Graph description |
   | `date_label`, `date_iso` | the date shown under the title (`<time datetime>`) |
   | `source_md` | the report, relative to `site/` |
   | `figures` | Markdown image filename -> SVG fragment path |
   | `links` | phrases to link: `section` (h2 text), `text`, `href`, `download` |
   | `appendix` | blocks to lift from other Markdown files: `from`, `start`/`end` marker lines (exclusive), `drop` (sentences that must exist once and are removed), `heading`, `after` (the h2 to insert after), `note` |
   | `downloads` | `csv`: `{from, name}` copied into `dist/<slug>/`; `pdf`: the PDF file name |
   | `pdf_must_contain` | strings `pdf.py` requires in the PDF text (use chart text) |
   | `checks` | the `verify.py` expectations file |

3. If the piece has a method appendix, add a panel to `method.panels` (below).
4. Write `checks/<slug>.json` for `verify.py`.
5. Rebuild. The root page's list, the sitemap, the masthead's "Work" state and the
   PDF follow from `site.json`. If it was listed as in progress, delete that entry
   (or turn it into the published one) in the same change.

## In-progress entries

A work that is announced but not published is a `works` entry with only a `title`,
an optional `descriptor` and `"status": "in_progress"`. It gets no page, no sitemap
entry and no PDF; the root page lists it unlinked and muted, with the
`root.in_progress_label` ("In progress") after the title and the descriptor, if any,
on the line below. Any other key on an in-progress entry (a slug, a source) fails
the build, so a half-configured piece cannot publish by accident. `pdf.py` skips
these entries.

## The method page

`content/method-intro.md` is authored copy and is rendered as written (Markdown,
with typographic quotes). It must open with `# Method`. Each entry in
`method.panels` then adds an `<h2>` and a block lifted from that work's `METHOD.md`
at build time:

| key | meaning |
|---|---|
| `work` | the published work's slug; also the section id (`/method#adv`) and the prefix of its heading ids (`#adv-limits`) |
| `heading` | the section's `<h2>` |
| `from` | the Markdown file, relative to `site/` |
| `run_line` | prefix of the one line to lift first (`Run: `); its code span must render verbatim |
| `start` | the heading line where the lifted block starts; it runs to the end of the file |
| `heading_level` | every heading in the block is set to this level (3) |
| `drop` | sentences removed from the block; matched after collapsing each paragraph's hard wraps, each must occur exactly once |
| `rename_headings` | heading text -> replacement, each must match exactly one heading |

Everything else in the block is reproduced as it stands, tables and code blocks
included. When `METHOD.md` changes so that a marker, a dropped sentence or a renamed
heading no longer matches exactly once, the build stops.

GitHub links in `content/*.md` are checked against `repo_url` and `repo_branch`,
and the file they name must be tracked by git.

## The data page and its stale-set expectation

`content/data-intro.md` is authored copy, rendered as written; it must open with
`# Data`. Below it, `build.py` reads `../archive/MANIFEST.csv`, checks every row
(ISO dates, a known `variant_by_content` and `source`, a 64-hex sha256, an SEC URL
ending in the file name, no file listed twice), and writes one table per entry in
`data.sections`, newest file first. `source` `backfill` is shown as "delisted".
The files in `data.panel_files` are set in bold, and their manifest sha256 must
appear in `data.panel_checksums_in` (the report's `METHOD.md`), so the page and the
report cannot disagree about which bytes were compared.

A file is stale when its `data_through` is no later than the latest `data_through`
among earlier-vintage files of the same variant: its data does not advance. Stale
files get a dagger. The build prints the stale set and fails unless it equals
`data.expected_stale` (currently `["ia09022025.xlsx"]`). When the weekly archive
job adds a file and the set changes, the build stops on purpose: look at the new
file, and only then update `expected_stale` (and `checks/pages.json`, which
`verify.py` holds to the same set).

## Typography notes

Inline code wraps at its spaces but never at a hyphen inside a token (`build.py`
marks those tokens `nobr`), so `--prior` or a file name is never split across
lines. Chart labels get a page-coloured halo (`.chart text` in `style.css`) so a
label over a bar or the dashed baseline stays legible in light, dark and print.
In the PDF, Chrome embeds each stroked glyph as a Type3 outline, which takes the PDF
from about 0.24 MB to about 1.6 MB. The site contract asks for the halo in print too,
and check K2 in `verify.py` fails on any `@media` rule that would win the cascade and
switch it off. So `@media print { .chart text { stroke: none; } }` would get the
smaller file back, but only as a deliberate change to the contract, with K2 relaxed
for print in the same change.

## Notes on headless Edge on Windows

`msedge.exe` is a launcher that can return before the browser has finished, so
`pdf.py` waits for a complete PDF instead of trusting the exit. Passing
`--blink-settings=scriptEnabled=false` makes headless Edge and Chrome produce no
output at all (no PDF, no screenshot, empty `--dump-dom`); emulate "JavaScript
off" with a `Content-Security-Policy: script-src 'none'` header or meta tag
instead. A headless window narrower than about 500 px is widened silently; render
narrow viewports inside an iframe of the target width.
