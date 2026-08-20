"""Read-only application bootstrap for a verified dependency state."""

from __future__ import annotations

import sys

from lockstep.dependency_patch import DependencyPatchError, verify_dependency_patch


def main() -> int | None:
    try:
        verify_dependency_patch()
    except DependencyPatchError as exc:
        print(
            f"Lockstep dependency patch verification failed: {exc}. "
            "Run lockstep-dependency-install.",
            file=sys.stderr,
        )
        return 1

    from lockstep.cli import main as cli_main

    return cli_main()
