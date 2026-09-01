# Final Review Fix Round 1 Report

## Scope and authority

This round executed the binding `final-fix-round-1-brief.md` from exact base
`bb8dd250a03458e88cbcef4d025abdcc26498d63` in the persistent
`lockstep-workflow-dsl` worktree. It changed local implementation, tests, and
governing evidence only. No push, publication, issue/PR, merge, tag, Task 12C,
or worktree delete/move/recreate/clean occurred.

## Closed findings

- Shared Workflow YAML ingress now rejects depth greater than 64, more than
  50,000 nodes, more than 10,000 entries in one collection, and more than 2 MiB
  aggregate scalar UTF-8 bytes before recursive node construction. Every limit
  yields stable `LSW111`, source mark, empty pointer, and reduction guidance;
  defensive recursion failures are translated to the same diagnostic family.
- Authoring and template dependency topology now traverses parsed typed
  executable IR only, in first-occurrence declaration order. Inert call-shaped
  metadata cannot add dependencies or outputs, and packaged source bytes are
  captured once per role.
- One pure collision classifier serves minimal initialization and packaged
  templates. It permits only zero occupied targets or a strict proper canonical
  prefix with exact planned bytes and modes. Full, holed, byte-mismatched, and
  mode-mismatched sets remain collisions through the original public error
  classes and messages.
- Every existing cut phase and target ordinal proves strict-prefix convergence
  through the same public initializer. The missing unchanged present-compile
  cut proves old bytes, modes, and inodes remain exact before first mutation and
  real runtime admission accepts the exact old DAG.
- Blocking legacy checks now inspect only the current canonical
  path/device/inode namespace under the existing authoring lock, presence-only
  and without parsing or mutation. `lockstep doctor` separately performs a
  read-only scan of at most 256 namespaces, emits neutral original-project
  guidance for orphan evidence, and degrades actionably on overflow.
- Unused imports, obsolete collision oracles, cwd-sensitive evidence pathspecs,
  stale 0/0 arithmetic, and over-broad regeneration wording were corrected.

## TDD evidence

| Cluster | RED | GREEN |
| --- | --- | --- |
| A — Workflow YAML bounds | 5 failed: four literal independent bounds were absent and the public mutation-free compile oracle exited successfully | 5 passed in 5.05s |
| B — typed topology | 3 failed: raw mapping traversal admitted inert metadata and template sources were reopened | 3 passed in 2.86s |
| C/D — strict prefix and crash oracle | 12 failed / 9 passed: all 12 valid regeneration cells collided; invalid/full guards and the existing admission oracle already held | 21 passed in 96.14s; exhaustive crash suite later 53 passed in 214.85s |
| E — exact-project v4 and doctor | 3 failed: project-global refusal blocked unrelated/replaced projects and 257 namespaces raised `StorageLimitExceeded` | 3 passed in 4.02s; full legacy/hooks verification later passed |

The first universal architecture run exposed the iterative-topology refactor and
two physical caps: 150 passed / 2 failed. After extracting a bounded typed
preorder traversal and moving cross-namespace discovery into the doctor-owned
module, the gate passed 151/151 without weakening a cap. The crash test is
299/300 lines; production files all remain within their frozen caps.

## Final measurements

- Production authoring: exactly 8 modules, 1,779 LOC (Gate P baseline 4,847;
  reduction 3,068). Per-file counts: 266, 224, 175, 306, 150, 221, 330, 107.
- Focused authoring tests: exactly 15 files, 2,065 LOC (baseline 8,843;
  reduction 6,778).
- Corrected cwd-independent top-anchored integration-consumer diff against
  `675c5bd`: 58 additions, 87 deletions, net -29. Per-file additions/deletions:
  templates 23/34, runtime service 6/49, CLI 14/2, MCP server 15/2, template
  installation 0/0.

The retired authoring lifecycle-family production scan returned no match. The
old-DTO production scan returned no match; its only focused-test match is the
harmless descriptive fixture-test name
`test_leaf_replanner_captures_present_before_and_changed_after_images`, not a
retired field or production symbol.

## Final verification

All Python/test commands ran from `lockstep/engine` with `uv run --no-sync`.

| Gate | Exact result |
| --- | --- |
| `python -m compileall -q src/lockstep` | exit 0 |
| directly changed/adversarial suites | 165 passed in 315.37s |
| `tests/test_authoring*.py tests/test_template_authoring_write.py` | 189 passed in 363.03s |
| workflow schema + universal architecture + installed recipe/template/CLI/server + doctor suites | 333 passed, 1 warning in 40.50s |
| full engine `pytest -q` | 1,708 passed, 1 skipped, 1 warning in 687.98s |
| `git diff --check` | exit 0 |

The sole warning is the pre-existing non-authoring 80-line cohesion warning for
`EffectCoordinator._reconcile_publication`.

## Threat-model and architecture self-review

- **SI-02 / SI-25:** explicit event-level YAML ceilings precede recursive node
  construction and public failure tests prove no project mutation.
- **SI-04:** typed topology and exact old/new DAG admission preserve deterministic
  definition binding; inert metadata is not control.
- **SI-20:** regeneration uses captured targets only; the publisher retains its
  descriptor-relative containment/currentness revalidation under lock.
- **SI-26:** present legacy evidence blocks only its exact authority-bearing
  namespace; read-only doctor inspection may degrade without inventing recovery.
- Graph-based change review reported broad indirect reach but no concrete
  uncovered flow. Its lexical test-gap hints are false negatives for the
  parameterized public crash, topology, legacy, and doctor suites listed above.

No frozen public facade/class signature, output, error class, eight-module
architecture, lifecycle, migration, or recovery behavior was added. Parent-owned
fresh independent product, architecture, and threat reviews remain the next
step before any publication decision.
