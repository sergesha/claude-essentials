# Task 7 manual/pinned effects and public controls report

## Scope and architecture

Task 7 bridges manual and pinned effects through the existing native
`GraphRuntime` → `EffectCoordinator` → `EffectLedger` boundary. LangGraph remains
the sole workflow/checkpoint authority. Public status and wait are projections;
there is no status mutation API, provider workflow table, pinned scheduler, or
second runner lifecycle.

The protected manual path durably captures a bounded project baseline and exact
coordinate/descriptor/write contract before worker ownership becomes visible.
`scenario_done`, `scenario_escalate`, and `scenario_abort` require the exact live
session and a session lease, submit one closed payload to the coordinator, seal
the existing effect row, and deliver only through the native resume boundary.
Manual work performs no spawn/cancel/quiesce operation and claims no transient
host containment.

Pinned commands are a thin strategy over the single Task 6 Codex attempt driver,
attempt directory, supervisor, receipt, and terminal-safety lifecycle. The
provider admits a closed logical argv/cwd contract, constructs the exact
credential-free `codex sandbox ... -- <argv>` command, uses a committed
`no_publish_operation` workspace purpose, and permanently quarantines the
workspace. It never rolls over, releases, reuses, or publishes pinned output.
The local profile states full `os_user_execution`; exit-only results do not claim
result stability, and file/JUnit result modes reject before launch.

## Crash recovery and resource boundaries

The service owns one ephemeral completion/recovery pump over the existing
coordinator. Its local active queue is hard bounded and overflow remains durably
discoverable through bounded rotating EffectLedger batches. Concurrent delivery
owners hold deterministic exact effect leases across sealed-fact revalidation,
native resume, lineage proof, and `mark_delivered`, so a competing owner defers
without repeating the native commit.

A presence-only start-admission outbox closes the cross-database crash cut
between a committed native start checkpoint and the first coordinate-bound
effect row. The outbox lives at the EffectLedger storage boundary and contains
only `public_run_id`, immutable bounded input BlobRef fields, and `admitted_at`;
it contains no phase, node, route, status, checkpoint, result, or launch
authority. Immutable RunCatalog binding and admission are inserted in one owner
SQLite transaction. `GraphRuntime.ensure_started` snapshots under the existing
invoke lock/lease: an existing checkpoint is adopted without replay, while an
empty native state receives the exact stored input once. Recovery limit 128 is a
batch size, not a correctness cap. Admission is acknowledged only after terminal
or ordinary-human native state, or after every current protected coordinate (and
manual handoff where applicable) is durably registered.

Project/manual manifests and Git attestations share `ProjectTreeLimits`. Entry,
depth, file, aggregate, refs/config/index, and marker reads are bounded before
sorting or hashing, closing the SI-25 iterator-growth paths without a manual-only
preflight or duplicated manifest implementation.

## Threat-model and review disposition

Independent pre-code review returned CONDITIONAL GO and required durable manual
handoff-before-ledger visibility, exact session/coordinate binding, a single
shared Codex attempt driver, and no-publish pinned quarantine. Successive reviews
found and closed reachable correctness/availability gaps in descriptor matching,
engine-owned completion delivery, scope preservation, bounded project/Git
capture, concurrent delivery, recovery scanning, active capacity, and the native
start crash cut.

The approved start-admission outbox is a temporary immutable-command delivery
bridge, not workflow status: it cannot select routing, project public status, or
authorize provider launch. The final independent review of the current tree
returned **ZERO** reachable threat-model/design findings.

## TDD and verification

RED commits:

- `3aa9e20` — public manual/pinned controls;
- `3e71a99` — coordinator-owned manual delivery;
- `c8474cc` — protected-manual bypass rejection;
- `5bb6332` — pinned runner boundary;
- `818de7d` — safe pinned status projection;
- `50432bb` — pinned launch-failure distinction;
- `c03454b` — manual recovery and project limits.

GREEN implementation commit: `6957cf2` (`feat(runtime): bridge manual and pinned native effects`).

- Final service/graph/ledger crash/concurrency gate: `47 passed`.
- Expanded Task 7 providers/effects/service/engine/session/validator gate:
  `247 passed`.
- Final complete offline suite: `667 passed, 1 skipped`.
- Ruff format/check over every changed Task 7 source and test: clean.
- `compileall` over `src` and `tests`: clean.
- `git diff --check`: clean.
- Independent final conformance review: ZERO.

No dependency mutation, network access, push, publication, fork, PR, or GitHub
operation was performed.
