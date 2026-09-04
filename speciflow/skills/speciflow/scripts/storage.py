"""Minimal SpeciFlow project storage resolver."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

FIELDS = ("version", "project_identity", "project_key")


def project_identity(project: Path) -> Path:
    project = project.expanduser().resolve(strict=True)
    run = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--git-common-dir"],
        check=False, capture_output=True, text=True,
    )
    if run.returncode != 0:
        return project
    common = Path(run.stdout.strip())
    return (project / common if not common.is_absolute() else common).resolve(strict=True)


def storage_base(project: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    default = Path.home().resolve(strict=True) / ".speciflow"
    for parent in (project, *project.parents):
        candidate = parent / ".speciflow"
        if candidate != default and candidate.is_dir():
            return candidate.resolve()
    return default


def values(project: Path, explicit: Path | None) -> tuple[dict[str, object], Path]:
    project = project.expanduser().resolve(strict=True)
    identity = project_identity(project)
    key = hashlib.sha256(os.fsencode(str(identity))).hexdigest()
    base = storage_base(project, explicit)
    root = base / "projects" / key
    metadata = root / ".speciflow-project.json"
    expected = {"version": 1, "project_identity": str(identity), "project_key": key}
    return {"project_identity": str(identity), "project_key": key, "storage_base": str(base),
            "data_root": str(root), "metadata_path": str(metadata), "expected_metadata": expected}, metadata


def metadata_status(path: Path, expected: dict[str, object]) -> str:
    if not path.exists():
        return "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "invalid"
    return "valid" if value == expected else "invalid"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("resolve", "init"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)
        command.add_argument("--base", type=Path)
    args = parser.parse_args(argv)
    try:
        output, metadata = values(args.project, args.base)
        status = metadata_status(metadata, output["expected_metadata"])
        if args.command == "init":
            if status == "invalid":
                print("metadata does not match expected project", file=sys.stderr)
                return 1
            if status == "missing":
                metadata.parent.mkdir(parents=True, exist_ok=True)
                with metadata.open("x", encoding="utf-8") as stream:
                    json.dump(output["expected_metadata"], stream, separators=(",", ":"), sort_keys=True)
                status = "valid"
        output["metadata_status"] = status
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
