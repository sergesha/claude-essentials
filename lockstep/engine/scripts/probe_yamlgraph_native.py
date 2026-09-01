#!/usr/bin/env python3
"""CLI wrapper for the adapter-owned yamlgraph native capability probe."""

from __future__ import annotations

import argparse

from lockstep.recipe.yamlgraph_adapter import probe_native_capabilities


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        probe_native_capabilities()
    except BaseException as exc:  # noqa: BLE001 - executable capability boundary
        if not args.quiet:
            print(f"yamlgraph native capability probe failed: {type(exc).__name__}: {exc}")
        return 1
    if not args.quiet:
        print("yamlgraph native capability probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
