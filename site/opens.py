#!/usr/bin/env python3
"""Who opened filingstrail.com: pageviews from Vercel Web Analytics, by hour.

    python opens.py                 # last 7 days, every page
    python opens.py --since 2d --path /adv
    python opens.py --since 30d --by referrer_hostname

Each line is one hour in which a page was viewed, with the viewer's country,
browser, OS and device, and where they came from. Web Analytics is cookieless:
there is no per-person identity, only these dimensions, so "opened" means a
pageview in the hours after the link went out from a plausible country/browser.

Counts only browsers that ran the (first-party) analytics script. A reader with
JavaScript disabled is not counted; neither are most mail-gateway link scanners,
which fetch the page without running scripts. Server-side request logs, which
would catch both, need Vercel Pro with Observability Plus.

Requires the Vercel CLI, logged in with access to the personal scope. Times are
printed in local time.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

PROJECT = "filingstrail"
SCOPE = "ronan-schultzs-projects"
METRIC = "vercel.analytics_pageview.count"
DEFAULT_BY = ["request_path", "country", "browser_name", "os_name", "device_type",
              "referrer_hostname"]


def query(since: str, by: list[str], path: str | None) -> dict:
    exe = shutil.which("vercel") or shutil.which("vercel.cmd")
    if not exe:
        raise SystemExit("vercel CLI not found on PATH")
    cmd = [exe, "metrics", METRIC, "--project", PROJECT, "--scope", SCOPE,
           "--prod", "--since", since, "--granularity", "1h", "--limit", "50",
           "-F", "json"]
    for dim in by:
        cmd += ["--group-by", dim]
    if path:
        cmd += ["-f", f"request_path eq '{path}'"]
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    out = run.stdout
    start = out.find("{")
    if start < 0:
        raise SystemExit(f"no JSON from vercel metrics:\n{run.stdout}\n{run.stderr}")
    data = json.loads(out[start:])
    if "error" in data:
        raise SystemExit(f"vercel metrics error: {data['error'].get('message')}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="7d", help="relative (1h, 2d, 30d) or ISO date")
    ap.add_argument("--path", help="only this page, e.g. /adv")
    ap.add_argument("--by", nargs="+", default=DEFAULT_BY, help="dimensions to break out")
    args = ap.parse_args()

    data = query(args.since, args.by, args.path)
    key = next((k for k in (data.get("summary") or [{}])[0] if k.endswith("_sum")),
               "vercel_analytics_pageview_count_sum")
    rows = [r for r in data.get("data", []) if r.get(key)]
    if not rows:
        print(f"No pageviews in the last {args.since}"
              + (f" on {args.path}" if args.path else "") + ".")
        return 0

    rows.sort(key=lambda r: r["timestamp"])
    print(f"{'hour (local)':<17} {'views':>5}  " + "  ".join(args.by))
    for r in rows:
        t = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        local = t.astimezone().strftime("%Y-%m-%d %H:%M")
        dims = "  ".join(str(r.get(d) or "-") for d in args.by)
        print(f"{local:<17} {r[key]:>5}  {dims}")
    total = sum(r[key] for r in rows)
    hours = len({r["timestamp"] for r in rows})
    print(f"\n{total} pageview(s) across {hours} hour(s), last {args.since}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
