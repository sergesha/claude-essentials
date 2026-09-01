# Task 12C baseline evidence

Execution start: `bcf944b6e59e436cb6fcc4c265ba365b490eaa2e`

Normative code baseline:
`4674e43fa1ffef1b9013f29345b2c7934808131e`

## Clean state and focused architecture baseline

From `lockstep/engine`:

```text
$ git status --short --branch
## feat/lockstep-workflow-dsl

$ git rev-parse HEAD
bcf944b6e59e436cb6fcc4c265ba365b490eaa2e

$ uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
151 passed
```

The obsolete line-based warning from that historical run is intentionally not
retained as architecture evidence and has since been removed from the gate.

## Full-suite baseline — verified

The controller resolved the earlier partial-output artifact. The outer
orchestration had completed while its nested PTY session remained live; a first
retry also leaked `UV_PROJECT_ENVIRONMENT` into a nested `uv` subprocess. The
isolated run used source commit `bcf944b`, a temporary worktree `.venv`
symlink to the installed environment, no `UV_PROJECT_ENVIRONMENT`, and from
`/private/tmp/lockstep-task-1-baseline/lockstep/engine` ran:

```text
uv run --no-sync pytest -q
```

It exited 0 with the pass/skip result below. The obsolete line-based warning
from the historical renderer output is intentionally omitted:

```text
1708 passed, 1 skipped
```

The focused reproducer for the environment-leak-induced false failure also
passed after removing the leak:

```text
uv run --no-sync pytest tests/test_dependency_patches.py::test_black_box_uv_run_fails_closed_after_original_or_mixed_sync_state -q
1 passed in 20.52s
```

## Reproducible population inventory

Commands from `lockstep/engine`:

```text
git ls-files 'src/lockstep/**/*.py' 'src/lockstep/*.py' | sort
git ls-files 'tests/**/*.py' 'tests/*.py' | sort
```

The exact NUL-safe file-counting commands were run in the
detached `bcf944b` baseline worktree:

```text
git ls-files -z 'src/lockstep/**/*.py' 'src/lockstep/*.py' | tr -cd '\000' | wc -c
git ls-files -z 'tests/**/*.py' 'tests/*.py' | tr -cd '\000' | wc -c
```

Their outputs were respectively `105` and `134`.

| Population/scope at execution start | Count |
| --- | ---: |
| Tracked `src/lockstep` Python files | 105 |
| Tracked test Python files | 134 |

The only pre-existing diff versus the normative baseline was Task 12C
planning/specification/review/ledger documentation; it contained no production
or test paths. Physical line counts and patch-size arithmetic are intentionally
not collected or used by Task 12C.
