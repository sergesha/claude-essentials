#!/usr/bin/env python3
"""Export a Tech Radar JSON data document to a human-readable YAML view.

JSON is the canonical storage format (written by collect-news). This produces an
optional YAML rendering for humans. OS-agnostic, standard-library only — it does
NOT require PyYAML (it emits a conservative YAML subset itself).

Usage:
  python3 export_yaml.py [path/to/radar-<stamp>.json]   # -> radar-<stamp>.yaml (+ radar-latest.yaml)
"""
import sys, json, re
from pathlib import Path


def _scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _emit(value, indent):
    """Yield YAML lines for a value already introduced by its key/dash."""
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        for k, v in value.items():
            lines += _emit_pair(k, v, indent)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                inner = []
                for k, v in item.items():
                    inner += _emit_pair(k, v, indent + 1)
                # replace the leading pad of the first inner line with "- "
                first = inner[0]
                inner[0] = pad + "- " + first[len(pad) + 2:]
                lines += inner
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def _emit_pair(key, value, indent):
    pad = "  " * indent
    if isinstance(value, str) and "\n" in value:
        body = value.rstrip("\n").split("\n")
        return [f"{pad}{key}: |"] + [f"{pad}  {ln}" for ln in body]
    if isinstance(value, dict):
        return ([f"{pad}{key}:"] + _emit(value, indent + 1)) if value else [f"{pad}{key}: {{}}"]
    if isinstance(value, list):
        return ([f"{pad}{key}:"] + _emit(value, indent + 1)) if value else [f"{pad}{key}: []"]
    return [f"{pad}{key}: {_scalar(value)}"]


def to_yaml(data):
    lines = []
    for k, v in data.items():
        lines += _emit_pair(k, v, 0)
    return "\n".join(lines) + "\n"


def main():
    argv = sys.argv[1:]
    to_stdout = "--stdout" in argv
    pos = [a for a in argv if not a.startswith("--")]
    src = Path(pos[0]) if pos else Path("reports/radar-latest.json")
    if not src.exists():
        sys.exit(f"No data document at {src}. Run /tech-radar:collect-news first.")
    data = json.loads(src.read_text())
    if to_stdout:
        sys.stdout.write(to_yaml(data))
        return
    stamp = src.stem.replace("radar-", "")
    out = src.parent / f"radar-{stamp}.yaml"
    out.write_text(to_yaml(data))
    (src.parent / "radar-latest.yaml").write_text(to_yaml(data))
    print(f"YAML view written: {out} (latest: {src.parent / 'radar-latest.yaml'})")


if __name__ == "__main__":
    main()
