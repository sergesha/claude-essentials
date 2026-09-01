# Task 12A microcycle 2 — transactional recipe initialization

Date: 2026-08-28

## Outcome

Microcycle 2 is complete. `write_compilation` and public CLI/MCP `recipe init`
now require or resolve explicit owner state and publish through the shared
authoring transaction. No direct project writer remains on those paths.

## Local commits

- `e3f49fa` — test-only RED: transactional recipe initialization contract.
- `34a6acc` — test-only RED: exact recovery order and owner-state binding.
- `9c040e5` — production GREEN: shared transaction cutover.

## Implemented boundary

- `initialize_minimal(project, name, *, state_dir)` executes one
  `recover → captured plan → all-destinations-absent check → publish` path.
- Workflow source, root recipe, dependency manifest, source map, and generated
  files are one immutable destination-only bundle.
- `write_compilation(recipe, *, state_dir)` executes recovery before the
  existing whole-DAG filesystem plan and publishes that single retained plan.
- CLI uses configured owner state; MCP resolves the same configured owner state.
  Existing stdout and MCP result schemas are unchanged.
- The captured-workflow installation planner is neutral core vocabulary;
  template installation is a thin facade over it.

## Verification

- Focused CLI/MCP/template/architecture gate: `84 passed`, one unchanged
  reviewed complexity warning.
- Expanded authoring range: `168 passed`, same warning.
- Packaging tests requiring nested `uv` network access: `2 passed`.
- Final full engine suite with `RLIMIT_NOFILE=4096`: `1458 passed, 1 skipped`,
  one unchanged warning for `EffectCoordinator._reconcile_publication`.
- `compileall`, import smoke, and staged/working diff checks passed.
- Architecture, threat-model, and reliability reviews: PASS, C0/I0/M0.

## Scope boundary

No durability-cut matrix, foreign mutation policy, concurrency matrix, or
check/diff/canonical plan binding was pulled into this microcycle. The next
approved leaf is microcycle 3: bind check, diff, and canonical observation to
one captured whole-DAG plan/result.
