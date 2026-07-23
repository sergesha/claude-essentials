#!/usr/bin/env python3
"""Show a stored Tech Radar result in a requested format.

JSON is the canonical store (reports/radar-<stamp>.json). This selects one run
and emits it as JSON (default), YAML, or HTML to STDOUT — so a human OR an
external agent can request a result in the format they need.

Selection:
  - no date         -> the latest run
  - --date <D>      -> the run whose timestamp is CLOSEST to D (exact match not
                       required). D = YYYY-MM-DD or YYYY-MM-DD-HHMM.

Format (--format): json (default) | yaml | html. YAML/HTML are produced by the
sibling stdlib scripts (export_yaml.py / render.py); nothing is hand-written and
no 'latest' pointer is touched.

Usage:
  python3 show.py                         # latest, JSON, to stdout
  python3 show.py --format html           # latest as HTML
  python3 show.py --date 2026-06-10 --format yaml
"""
import sys, subprocess
from datetime import datetime
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
RENDER = SKILLS / "render-dashboard" / "render.py"
EXPORT = SKILLS / "collect-news" / "export_yaml.py"
REPORTS = Path("reports")


def parse_stamp(name):
    # radar-2026-06-14-0810.json -> datetime
    s = name.replace("radar-", "").replace(".json", "")
    for fmt in ("%Y-%m-%d-%H%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def parse_target(d):
    for fmt in ("%Y-%m-%d-%H%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            pass
    sys.exit(f"Bad --date '{d}'. Use YYYY-MM-DD or YYYY-MM-DD-HHMM.")


def main():
    argv = sys.argv[1:]
    fmt = "json"
    date = None
    i = 0
    while i < len(argv):
        if argv[i] == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]; i += 2
        elif argv[i] == "--date" and i + 1 < len(argv):
            date = argv[i + 1]; i += 2
        else:
            i += 1
    if fmt not in ("json", "yaml", "html"):
        sys.exit(f"Bad --format '{fmt}'. Use json|yaml|html.")

    runs = [(parse_stamp(p.name), p) for p in REPORTS.glob("radar-*.json") if p.name != "radar-latest.json"]
    runs = [(dt, p) for dt, p in runs if dt]
    if not runs:
        sys.exit("No stored results found. Run /tech-radar:collect-news first.")

    if date:
        target = parse_target(date)
        chosen = min(runs, key=lambda r: abs((r[0] - target).total_seconds()))
        note = f"closest to {date}"
    else:
        chosen = max(runs, key=lambda r: r[0])
        note = "latest"
    dt, path = chosen
    print(f"# result: {path.name} ({note}), format={fmt}", file=sys.stderr)

    if fmt == "json":
        sys.stdout.write(path.read_text())
    elif fmt == "yaml":
        subprocess.run([sys.executable, str(EXPORT), str(path), "--stdout"], check=True)
    else:  # html
        subprocess.run([sys.executable, str(RENDER), str(path), "--stdout"], check=True)


if __name__ == "__main__":
    main()
