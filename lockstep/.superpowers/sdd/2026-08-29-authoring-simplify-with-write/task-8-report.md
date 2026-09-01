# Task 8 Report — Final Evidence and Governing Documentation

## Scope

The original Task 8 changed no production or test file and updated only seven
governing documents. This report was amended by final fix round 1 to record the
corrected current-head implementation, tests, and measurements for the completed
`simplify-with-write` range. No push, publication, issue, PR, merge, tag, Task
12C work, or worktree cleanup occurred.

## Commit range

The implementation evidence retained in this range is:

- Task 1: `8cca4fb` — locked observation centralization.
- Task 2: `805bf7e`, `95937b4` — bounded per-file writer and failed-temporary
  cleanup.
- Task 3: `556b4c1`, `9ba6c7c`, `1850601` — cutover tests and identity oracle.
- Task 4: `ad279dd`, `faf0502` — public per-file-write cutover and observer
  contracts.
- Task 5: `070b4ff` — journal/recovery family deletion.
- Task 6: `e1690c9` — containment-capture consolidation.
- Task 7: `f9b70bb` — final DTO cutover and pure-plan consolidation.

The original report was committed by the Task 8 documentation commit
`docs(authoring): record simplify-with-write evidence`; final fix round 1
supersedes its active arithmetic and reliability claims.

## Measured Gate P arithmetic

The exact commands were run from `lockstep/engine`:

```text
find src/lockstep -maxdepth 1 -type f -name 'authoring*.py' -print0 |
  sort -z | xargs -0 wc -l
find tests -maxdepth 1 -type f \
  \( -name 'test_authoring*.py' -o -name '_authoring*.py' -o \
     -name 'test_template_authoring_write.py' \) -print0 |
  sort -z | xargs -0 wc -l
```

| Population | Gate P baseline | Final | Reduction | Limit | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Authoring production | 4,847 LOC | 1,779 LOC / 8 modules | 3,068 LOC | <= 1,940 LOC / exactly 8 | Pass |
| Focused authoring tests | 8,843 LOC | 2,065 LOC / 15 files | 6,778 LOC | <= 2,980 LOC / exactly 15 | Pass |

The eight production files are `authoring.py`, `authoring_bundle.py`,
`authoring_capture.py`, `authoring_compilation.py`, `authoring_installation.py`,
`authoring_project_tree.py`, `authoring_publisher.py`, and
`authoring_results.py`. Their exact line counts are respectively 266, 224, 175,
306, 150, 221, 330, and 107. The architecture suite mechanically enforces this
file set, every frozen file cap, the total cap, and universal authoring function
metrics.

The 15 focused test files total 2,065 lines; all are within their frozen caps.

## Fixed integration consumers

The original Task 8 command used cwd-relative pathspecs from the wrong directory,
so its empty result was invalid. At the final-fix HEAD, the corrected
cwd-independent command uses `:(top)lockstep/engine/...` pathspecs for
`templates/__init__.py`, `template_installation.py`, `runtime/service.py`,
`cli.py`, and `mcp/server.py`. The exact additions/deletions are
`templates/__init__.py` 23/34, `runtime/service.py` 6/49, `cli.py` 14/2, and
`mcp/server.py` 15/2, with no diff for `template_installation.py`: **58
additions, 87 deletions, net -29**. Inspection confirms that no consumer owns a
new lifecycle, state machine, lock, or persistence mechanism.

## Retired-mechanism and DTO absence

The exact production-authoring lexical scan for `AuthoringJournal`,
`AuthoringTransaction`, `AuthoringRecovery`, `authoring-transaction/v[234]`,
`reserved_stage`, `restoration_stage`, `record_replacement`,
`record_committed`, and `rollback` returned no match.

The exact authoring production/focused-test identifier scan for
`ProjectCompilationBundle`, `SourceIdentity`, `DestinationImage`,
`authoring_identity`, `_plan_project_compilation`, `planned.bundle`,
`before_images`, and `after_images` returned no production match. The focused
test scan has one harmless descriptive fixture-test identifier,
`test_leaf_replanner_captures_present_before_and_changed_after_images`; it does
not reference a retired DTO field or production symbol. The final DTO family
remains the six contracts recorded by Task 7.

The focused legacy-refusal test intentionally embeds raw v2/v3/v4 schema bytes
as hostile/legacy input fixtures. They are not a parser, recovery path, alias,
or authoring production mechanism. A broader `src/lockstep` scan also finds
unrelated runtime artifact-publication and SQLite rollback vocabulary; Task 10
owns that distinct authority and it is outside this authoring deletion scope.

## Historical Task 8 verification evidence

All commands used `uv run --no-sync`, never the relocated `.venv/bin/pytest`:

| Command | Result |
| --- | --- |
| `python -m compileall -q src/lockstep` | exit 0 |
| `pytest tests/test_authoring*.py tests/test_template_authoring_write.py -q` | 166 passed in 172.67s |
| `pytest tests/architecture/test_no_god_methods.py -q` | 150 passed, 1 warning in 0.60s |
| `pytest tests/test_recipe_cli.py tests/test_templates.py tests/test_cli.py tests/test_server.py -q` | 122 passed in 21.22s |
| `pytest -q` | 1,676 passed, 1 skipped, 1 warning in 414.48s |

The sole warning in the architecture and full suites is the pre-existing,
non-authoring 80-line cohesion warning for
`EffectCoordinator._reconcile_publication`.

## Governing-document changes

- The Task 12 master plan and corrected replan now distinguish active per-file
  authoring reliability from their labelled historical journal-transaction
  design.
- The historical progress ledger records final counts, test evidence, the
  corrected five-consumer arithmetic, and that Task 12C remains blocked.
- README, DESIGN, and both installed skills state the same user-visible
  contract without promising multi-file atomicity or automatic recovery.

The active reliability wording is deliberately exact: cooperating writers
serialize; each file replacement is atomic and durable; a crash may leave old,
new, or mixed generated files; runtime start admits only a freshly observed
complete canonical closure and exact DAG. First-init/template reruns complete
only a strict proper canonical prefix with exact planned bytes and modes; full,
holed, or mismatched sets remain collisions. Normal operations enforce legacy
evidence only for the current exact project identity, while `lockstep doctor`
performs bounded read-only cross-namespace discovery. Legacy v4 requires a
pre-simplification recovery build and must not be manually deleted.

## Hygiene and handoff

`git diff --check` passed before the documentation commit. The final diff was
inspected for historical/active-contract separation. Three independent final
reviews remain assigned to the parent; this report does not self-approve the
range or authorize publication.
