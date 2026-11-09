#!/usr/bin/env python3
"""Run CRG with a complete Git-visible inventory, including untracked sources.

The adaptation is process-local: no installed dependency or Git index is edited.
Both CLI indexing and MCP auto-updates use this entrypoint.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing


def install_inventory_adapter():
    from code_review_graph import incremental

    if getattr(incremental.get_all_tracked_files, "_code_intel_visible", False):
        return incremental

    tracked_files = incremental.get_all_tracked_files
    changed_files = incremental.get_changed_files

    def untracked_files(repo_root):
        if incremental.detect_vcs(Path(repo_root)) != "git":
            return []
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=30,
        )
        return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def visible_files(repo_root, recurse_submodules=None):
        tracked = tracked_files(repo_root, recurse_submodules)
        return sorted(set(tracked).union(untracked_files(repo_root)))

    def visible_changes(repo_root, *args, **kwargs):
        return sorted(set(changed_files(repo_root, *args, **kwargs)).union(untracked_files(repo_root)))

    visible_files._code_intel_visible = True
    incremental.get_all_tracked_files = visible_files
    incremental.get_changed_files = visible_changes
    return incremental


def health(repo: Path, *, integrity: bool = False, database: Path | None = None) -> dict:
    incremental = install_inventory_adapter()
    repo = repo.resolve()
    db = database or incremental.get_db_path(repo, read_only=True)
    expected = incremental.collect_all_files(repo)
    result = {"database": str(db), "expected_files": len(expected), "indexed_files": 0, "issues": []}
    if not db.is_file():
        result["issues"].append("missing database")
        return result
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            if integrity:
                check = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
                if check != "ok":
                    result["issues"].append(f"SQLite integrity: {check}")
                    return result
            hashes = dict(connection.execute("SELECT file_path, file_hash FROM nodes GROUP BY file_path"))
            stored = set(hashes)
            result["indexed_files"] = len(stored)
            outside = [name for name in stored if not Path(name).is_relative_to(repo)]
            if outside:
                result["issues"].append(f"different repository root: {outside[0]}")
            removed = [name for name in stored if not Path(name).is_file()]
            if removed:
                result["issues"].append(f"{len(removed)} deleted indexed files, e.g. {removed[0]}")
            candidates = [name for name in expected if str((repo / name).resolve()) not in stored]
            # Recognized configuration formats may intentionally yield no
            # graph nodes (for example ordinary non-Spring YAML). Distinguish
            # that from a source file the indexer actually omitted.
            parser = incremental.CodeParser(repo)
            missing = []
            for name in candidates:
                nodes, _edges = parser.parse_file(repo / name)
                if nodes:
                    missing.append(name)
            if missing:
                result["issues"].append(f"{len(missing)} source files missing, e.g. {missing[0]}")
            stale = [name for name in expected if str((repo/name).resolve()) in hashes
                     and hashes[str((repo/name).resolve())] != hashlib.sha256((repo/name).read_bytes()).hexdigest()]
            if stale:
                result["issues"].append(f"{len(stale)} stale source files, e.g. {stale[0]}")
    except (sqlite3.Error, OSError) as error:
        result["issues"].append(f"SQLite: {error}")
    return result


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "location":
        incremental = install_inventory_adapter()
        print(json.dumps({"database": str(incremental.get_db_path(Path(sys.argv[2]).resolve(), read_only=True))}))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        database = Path(sys.argv[sys.argv.index("--database")+1]) if "--database" in sys.argv else None
        result = health(Path(sys.argv[2]), integrity="--integrity" in sys.argv[3:], database=database)
        if "--paths" in sys.argv:
            result["source_files"] = install_inventory_adapter().collect_all_files(Path(sys.argv[2]).resolve())
        print(json.dumps(result))
        return int(bool(result["issues"]))
    incremental = install_inventory_adapter()
    if len(sys.argv) > 1 and sys.argv[1] == "build-staged":
        repo, directory = Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        # --data-dir persists a registry redirect. A temporary build must never
        # change the active graph's location or migrate a legacy database.
        incremental.get_data_dir = lambda _repo, **_kwargs: directory
        incremental.get_db_path = lambda _repo, **_kwargs: directory / "graph.db"
        sys.argv = [sys.argv[0], "build", "--repo", str(repo)]
    if len(sys.argv) > 1 and sys.argv[1] == "serve-explicit":
        # Legacy Codex resolves relative cwd against the plugin root but does
        # not interpolate plugin-root placeholders in arguments. Never let
        # that bootstrap cwd become an implicit graph target.
        from code_review_graph import main as server

        def explicit_repo_root(repo_root):
            if not repo_root:
                raise ValueError("Code Intel: pass repo_root explicitly as the absolute path of the working project.")
            if not Path(repo_root).is_absolute():
                raise ValueError("Code Intel: repo_root must be an absolute project path.")
            return repo_root

        server._resolve_repo_root = explicit_repo_root
        sys.argv[1] = "serve"
    from code_review_graph.cli import main as native_main

    native_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
