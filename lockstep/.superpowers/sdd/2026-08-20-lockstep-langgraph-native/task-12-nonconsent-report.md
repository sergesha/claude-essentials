# Task 12 native runtime inputs and authoring — non-consent report

## Progress ledger

1. **Normative-input recovery:** the original ignored 2026-08-20 Task 12
   specification and plan had vanished with the prior temporary workspace.
   Per the owner ruling, this phase used `docs/DESIGN.md` as the authoritative
   native architecture, `docs/DESIGN-SUBCALLS.md` only as scheduled-for-deletion
   negative evidence, the committed Task 11 report/pre-code review, the six
   committed Task 12 RED test files, and the frozen dispatch rulings. The
   accepted threat stop-rule was: accept a finding only with a reachable
   external boundary plus an authority, correctness, or confidentiality delta;
   do not recurse into anti-reflection or TCB hardening.
2. **RED:** the exact focused Task 12 command initially produced `32 failed,
   12 passed`. Missing seams covered durable runtime snapshot assignment,
   runner-free decisions, observational events/wait, source freshness,
   package templates, public authoring surfaces, and native plugin identity.
3. **GREEN:** neutral append-only runtime-input facts close the Task 11 release
   gate; trusted decision evaluation consumes exact snapshots without a runner;
   status/wait/history/events stay bounded and observational; recovery is an
   explicit bounded mutation. The common authoring classifier now proves
   checked-in compiler output in memory before durable admission, while the
   closed template catalog installs a complete child DAG atomically.
4. **Scope boundary:** this commit deliberately does not change acceptance
   commitment semantics or add the consent-specific CLI/MCP/service surface.
   Those exact RED tests remain for the subsequent consent phase. No workflow,
   scheduler, route, join, timer, or status authority was added.
5. **Post-consent correctness fix round:** the scoped rereview accepted exactly
   three P1 findings. A single Workflow DSL logical-name validator now runs at
   every authoring/template entry before path construction or mutation;
   explicit recovery pages the effect ledger's nonterminal thread queue before
   mapping immutable catalog bindings; and one canonical on-disk recipe-byte
   calculation retains child links across template install, diff, compile, and
   canonical match. The recovery cursor is ephemeral scan progress, not a new
   workflow, scheduler, route, status, or effect-phase authority.
6. **Packaged-provider ruling:** the rereview's proposed generic/Claude runner
   selector was rejected. The native core remains provider-neutral, while the
   released `parallel-review` and `reviewed-change` package templates are
   intentionally Codex-managed because Codex is the only released managed
   provider. No unavailable provider contract, template parameter, or generic
   selector was invented. The older parity design is superseded and is owned
   by Task 13 documentation cleanup; current active documentation does not
   claim these package templates select the ambient host provider.

## Architecture delivered

`run_start_inputs` and `effect_runtime_inputs` are immutable external-fact
tables with their own neutral SQL metadata boundary. They are not graph state,
effect-ledger columns, a global latest snapshot, or a private-saver read. Start
admission binds the exact project snapshot in the catalog/dispatch transaction.
Current inputs select coordinate-ancestor successors and compute the greatest
common ancestor where concurrent native lineages meet. Manual completion,
publication, and verified runner rollover bind exact successors. Every lookup
rechecks the immutable run/project/definition/native-coordinate/descriptor
identity and the chain back to the run-start root.

`DecisionDescriptor` executes in the coordinator's trusted boundary without a
runner or effect row. Its closed changed-paths program compares exact start and
current snapshot manifests and resumes the exact native interrupt with a typed
`DecisionResult`.

Public history, events, status, and wait expose only bounded redacted native and
effect observations and never drive reconciliation. Event delivery failures
are warnings, not workflow facts. `scenario_recover` is the only new recovery
surface and performs one explicit bounded sweep.

The pure authoring path strictly loads, parses, resolves the conventional child
catalog, validates, and compiles before writing. A generated marker cannot fall
back to manual classification: its conventional source, root recipe,
specialized files, child DAG, and dependency manifest must match canonical
compiler bytes. The resulting in-memory `canonical-match` capability binds the
same complete canonical source bundle later admitted by strict recipe ingress.
Manual marker-free yamlgraph remains supported.

The package resource catalog is exactly `parallel-review` and
`reviewed-change`; every bundle manifest is closed. Template install rejects
custom paths, computes and preflights every source/recipe/compiler destination,
stages and compiles children before the parent, publishes under a bounded
validated recovery journal, and rolls back only exact journal-bound bytes.
CLI and MCP adapters remain thin delegates for authoring plus scenario
start/status/done/escalate/abort/wait/history/events/recover.

The correctness fix round moved logical-name validation to the earliest shared
authoring boundary and applies the Workflow DSL grammar before even an install
recovery journal can be consumed. Explicit recovery now asks the ledger for a
bounded, stable page of recoverable effect threads, maps each through the
immutable catalog, filters the ambient project, and advances an in-memory
per-project scan cursor. Terminal catalog rows therefore consume no recovery
budget, while ledger/native facts remain the only durable authorities. The
authoring module also owns the canonical linked root-recipe bytes used by all
four install/write/diff/match consumers, so recompilation cannot silently
discard the strict-ingress child DAG.

The legacy process-subcall design document was deleted as required negative
evidence. `docs/DESIGN.md` and the Codex plugin identity now describe native
durable workflows and external-effect bridging.

## Verification evidence

- Initial focused RED: `32 failed, 12 passed`.
- Fresh focused non-consent Task 12 matrix: `43 passed, 1 deselected` in
  `7.13s`; the deselection is the combined MCP assertion's consent-only
  `scenario_accept_artifact` expectation.
- Complete offline non-consent regression: `897 passed, 1 skipped, 3
  deselected` in `153.83s`. Besides the consent-containing MCP assertion, the
  two deselections are pre-Task-12 exact-equality assertions for the old Codex
  interface text and old 11-tool MCP set; the committed Task 12 tests assert
  their superseding contracts.
- The five uv-build/sync and nested-junit tests that fail in the restricted
  shell all passed under the authorized offline uv cache environment:
  `5 passed in 44.82s`.
- Explicit deferred/obsolete boundary probe: `7 failed` — four exact
  acceptance-commitment RED tests, the consent MCP registration expectation,
  and the two superseded exact-interface assertions. No consent code was added
  to make those tests pass.
- Installed `parallel-review` parent smoke preflight minted
  `canonical-match` over its complete five-file executable DAG.
- `python -m compileall -q src tests`: clean.
- `git diff --check`: clean.
- Core self-review found no legacy process-subcall, private saver, MemorySaver,
  or runner-provider import in the new snapshot resolver, authoring, template,
  or event modules.

Post-consent correctness fix evidence:

- Logical-name boundary RED: `33 failed`; GREEN: `33 passed`. Direct,
  CLI, and MCP cases cover traversal, absolute, slash, and backslash names and
  compare the complete probe tree before/after, including a live template
  recovery journal.
- Nonterminal-first recovery RED: `2 failed`; GREEN: `2 passed`. The real
  SQLite probe contains 128 older terminal catalog bindings and a later
  nonterminal effect; the separate paging probe proves stable progress across
  a foreign-project page.
- Independent scoped rereview found one capacity-deferral edge: an unreserved
  explicit drive could advance the cursor and report false recovery. Its exact
  RED was `1 failed`; GREEN was `1 passed`. Recovery now reserves before
  binding/driving, leaves cursor/result unchanged when capacity is full, and
  retries the same thread once capacity is available. Reviewer follow-up
  confirmed the issue resolved with no new production finding; the complete
  recovery group is `3 passed`.
- Canonical linked-recipe RED: `4 failed`; GREEN: `4 passed`, covering both
  packaged templates and rejection of an unlinked parent as canonical.
- Combined authoring/templates/CLI/MCP/service regression: `135 passed`.
- Owner-consent/publication/coordinator/lowering regression: `231 passed`.
- Complete locked offline suite: `983 passed, 1 skipped` in `317.91s`; the
  skip is the existing environment-dependent integration skip.
- Fresh `python -m compileall -q src tests` and `git diff --check`: clean.
- Ruff remained unavailable in the locked environment (`Failed to spawn:
  ruff`); dependencies were not changed to add it.
- Code-review-graph analyzed seven changed files at risk `0.60` with zero
  registered affected flows. Its syntactic gap list did not associate the
  parametrized direct/CLI/MCP tests with the shared helpers; the executable
  RED/GREEN and full-suite evidence above is authoritative.

Implementation/docs commit:
`385b721924898af25f225d9bd30b0badbfad6b9f` (`feat(lockstep): add native
authoring runtime surfaces`). No network access, dependency mutation, push,
publication, consent implementation, or sidecar workflow authority was used.

The post-consent correctness round likewise used no network, dependency
mutation, push, publication, provider-contract expansion, or new durable
workflow authority. Its local Conventional Commit is created only after this
report, final GREEN verification, and independent scoped rereview.
