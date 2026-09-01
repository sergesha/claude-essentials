# Task 12A.5 Complexity and Proportionality Audit

Date: 2026-08-29
Measured code population: `2765b0bd47ac98591e1321c3eedb0da6e0cd2835`
Code population verified unchanged through audit commit:
`12394ac6fba4e79edaf74d1d5f18c765fb62b628`
Decision status: awaiting independent reviews and explicit user selection
Recommended selection: **simplify-with-write** in a dedicated, separately
planned authoring-only range before Task 12C

## Product decision in one paragraph

Retain Lockstep's narrow product kernel and current authoring CLI contract:
deterministic Workflow DSL to
inspectable YAMLGraph compilation, strict bounded ingress and containment,
LangGraph as the sole workflow-state authority, exact owner grants and
currentness, and truthful external-effect commitment/reconciliation. Do not
retain the current crash-atomic multi-file authoring transaction as a v1 local
single-user guarantee. It is a reliability profile rather than a security
boundary. The exact current authoring surface is 4,847 production and 8,843
test lines; the causal B1 budget below removes at least 2,647 production and
5,643 test lines dominated by its journal, rollback, stage ownership,
directory recovery, and crash matrix. Replace it in a dedicated authoring-only
simplification range with bounded automatic writing, one authoring-specific
crash-released kernel lock, descriptor-relative per-target validation, and
per-file atomic replacement. An owner-applied staged bundle/patch is a separate
product variant, not an implementation detail. Task 12C must remain blocked
until the user approves one enumerated outcome and any selected remediation is
complete.

## Measurement definitions

- Lockstep production population: `engine/src/lockstep/**/*.py`.
- Lockstep test population: `engine/tests/**/*.py`.
- Physical lines: Python `splitlines()` count.
- Physical SLOC: nonblank, non-comment physical lines. Docstrings count as
  executable source.
- Logical lines: Python AST statement nodes.
- Dependency population: installed distribution `.py` files reported by
  `importlib.metadata.Distribution.files`.
- Public-interface counts are syntactic upper bounds. The supported Lockstep
  contract is the explicit exports, console scripts, CLI commands, MCP tools,
  and documented facades—not every lexically public definition.
- Installed dependency wheels contain no upstream tests. Their test LOC is
  therefore unavailable, not zero, and is excluded from proportionality claims.
- Whole-file capability assignment gives each file one primary owner. It is
  reproducible but intentionally does not pretend that cross-cutting files have
  only one caller.

Reproduction anchors:

```sh
git rev-parse HEAD
git status --short
find engine/src/lockstep -type f -name '*.py' -print0 | xargs -0 wc -l
find engine/tests -type f -name '*.py' -print0 | xargs -0 wc -l
engine/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("yamlgraph"), m.version("langgraph"))'
rg -n '\b(file_lock|advisory_file_lock)\(' engine/src/lockstep -g '*.py'
rg -n 'threading\.(RLock|Lock|Event)\(' engine/src/lockstep -g '*.py'
```

The detailed AST/token measurement used `ast.walk()` for logical statements,
module/class bodies for definitions, and `tokenize.COMMENT` for comment-only
lines. The audit retains both physical and logical measures because neither is
a reliable SRP proxy alone.

## Exact installed baseline

| Distribution/runtime | Exact version |
|---|---:|
| Python | 3.12.10 |
| SQLite | 3.47.1 |
| Lockstep | 0.1.0 |
| YAMLGraph | 0.5.22, locally and reproducibly patched |
| LangGraph | 1.2.10 |
| langgraph-checkpoint | 4.2.0 |
| langgraph-checkpoint-sqlite | 3.1.1 |
| langgraph-prebuilt | 1.1.0 |
| langgraph-sdk | 0.4.2 |
| SQLAlchemy | 2.0.52 |

The YAMLGraph wheel hash in `uv.lock` is
`923c324a6cfd3554eee9769abf75c472ae600680321bba5cfb83918602712f58`.
The installed source is `fully patched`; the patch manifest binds patch digest
`2af7f84c2663be4d8416e3a6e0f648b823a89d3b0ae15ee9daaf0c8e4d32d2d6`.
Patch growth has a zero budget and a deletion trigger when the pinned upstream
release supplies the required native subgraph/config propagation.

## Comparable size measurements

### Lockstep production and tests

| Surface | Files | Physical | Physical SLOC | Logical AST statements |
|---|---:|---:|---:|---:|
| Production Python | 117 | 39,455 | 35,302 | 18,997 |
| Test Python | 147 | 46,627 | 40,078 | 21,414 |
| Installed non-Python assets | 9 | 228 | n/a | n/a |
| Test fixtures/non-Python data | 45 | 2,353 | n/a | n/a |

The test/production ratios are 1.182 physical, 1.135 physical SLOC, and
1.127 logical. Eight modules declare 53 unique explicit export names. The
installed surface also contains 2 console scripts, 29 CLI leaf commands plus
`--version`, 24 MCP tools, 2 public `Engine` constructors, 2 templates, 7
template YAML files, and the dependency patch manifest/patch.

### Exact installed dependency production source

| Distribution | Python files | Physical lines |
|---|---:|---:|
| YAMLGraph 0.5.22, patched | 141 | 25,849 |
| LangGraph 1.2.10 | 78 | 27,872 |
| langgraph-checkpoint 4.2.0 | 17 | 5,894 |
| langgraph-checkpoint-sqlite 3.1.1 | 8 | 3,941 |
| langgraph-prebuilt 1.1.0 | 7 | 3,646 |
| langgraph-sdk 0.4.2 | 45 | 18,729 |

Lockstep production is larger than either patched YAMLGraph or LangGraph core.
That fact is a review trigger, not by itself a defect. The defect would be
retaining duplicated mechanisms without a unique acceptance requirement and
reachable threat path.

## Lockstep capability partition

| Primary capability | Production files / physical / logical | Test files / physical / logical |
|---|---:|---:|
| Workflow compilation | 19 / 8,520 / 4,250 | 19 / 6,579 / 2,161 |
| Runtime supervision | 49 / 13,686 / 6,869 | 47 / 11,836 / 5,794 |
| Authority/provisioning | 12 / 7,517 / 3,083 | 21 / 9,484 / 4,083 |
| Durable driving | 5 / 2,580 / 1,044 | 23 / 7,630 / 3,367 |
| Authoring transaction/recovery | 20 / 4,847 / 2,510 | 28 / 8,843 / 4,890 |
| CLI/MCP | 5 / 1,659 / 863 | 4 / 1,121 / 583 |
| Installed contract | 7 / 646 / 378 | 5 / 1,134 / 536 |

The five largest production files total 9,700 physical lines (24.6%): effect
coordinator 3,100, workflow lowering 2,811, Codex provider 1,444, command
service 1,235, and effect ledger 1,110. Their methods are now structurally
guarded where independently adjudicated, but file and subsystem proportionality
still requires product-level review.

## Persistence and lifecycle inventory

Lockstep owns 11 current runtime SQLite tables:

`runs`, `run_start_inputs`, `effect_runtime_inputs`, `consent_epochs`,
`publication_consents`, `leases`, `effects`, `effect_observations`,
`run_drive_watches`, `runtime_schema_migrations`, and `runtime_schema_epoch`.

There are two explicit recovery-journal families: authoring transaction
(`v2`, `v3`, current `v4`) and publication (`v1`). A lexical scan finds 67
versioned `lockstep.../vN` tags across 65 domain families; this is an upper bound
over durable records, wire documents, DTO schemas, and digest domains—not 67
database schemas.

Conservative independent custom lifecycle count:

1. effect ledger/outbox;
2. lease/fence epochs;
3. run-drive watch/migration;
4. owner snapshot generations/current pointer;
5. publication consent/epoch;
6. workspace lease/lifecycle;
7. Codex attempt/supervisor;
8. publication journal;
9. authoring transaction journal;
10. command-service activation/close/pump;
11. recovery sweep/cursor scheduling.

LangGraph workflow/checkpoint state is correctly excluded: it is supplier-owned
and remains the sole logical workflow authority.

Locking includes a 57-line verified `flock` primitive, a separate 296-line
`O_EXCL` plus wall-clock stale-breaking protocol, 23 cross-process lock call
sites across 17 logical namespaces, three service mutexes, and SQLite
transactions/CAS. Because the product is already POSIX-only through `fcntl`,
the stale-breaking sidecar protocol is a future replacement candidate unless a
real unsupported use case is demonstrated. It is global runtime infrastructure
with non-authoring call sites and is explicitly outside the first authoring
simplification range.

## Requirement and threat attribution

| Capability | Current product requirement | Reachable frozen threat paths | Disposition |
|---|---|---|---|
| Strict Workflow DSL and canonical YAMLGraph compilation | Deterministic inspectable recipes and immutable transitive DAG | SI-02, SI-04, SI-25, SI-28 | Retain; simplify lowering where equivalence is proven |
| LangGraph graph/checkpoint adapter | One workflow state, pause/resume/history/restart | SI-07, SI-10, SI-11 | Retain narrow adapter; no second scheduler |
| Owner snapshot/grant/currentness | Configuration never mints authority; exact binding and revocation | SI-01, SI-14–SI-19, SI-29 | Retain |
| Effect commitment and reconciliation | Truthful external send/process outcomes and consent correlation | SI-07–SI-16, SI-29 | Retain narrow kernel |
| Runtime supervision | Fixed providers, bounded argv/output, workspace and receipt truth | SI-05–SI-13, SI-21–SI-23 | Retain; simplify reliability-only lifecycle layers |
| Durable watch/migration/fairness | Restart progress and bounded local recovery | SI-08, SI-12, SI-26, SI-27 | Simplify; cross-project SLO/distributed scheduling is out of scope |
| Authoring capture/planning | Exact bounded source closure and deterministic child-first outputs | SI-02, SI-04, SI-20, SI-25, SI-26 | Retain |
| Authoring multi-file transaction | All-old/all-new project mutation across crash cuts | Reliability-only for cooperating local writers | Defer/remove from v1 |
| CLI/MCP/hook correlation | Uniform bounded public ingress and session/project binding | SI-05, SI-06, SI-22, SI-24 | Retain only supported installed surfaces |
| Dependency patch installer | Exact missing YAMLGraph native-subgraph prerequisite | SI-04, SI-19, SI-26 | Replace/delete on fixed upstream release |

No large current subsystem is completely unattributed under the existing
acceptance suite. That does not make each guarantee proportionate. The current
acceptance requirement for crash-atomic authoring is itself the candidate being
re-scoped because it has no attacker authority delta under the selected local
profile and dominates its product capability's cost.

## Existing primitive versus custom mechanism

| Existing primitive | Sufficient delegation | Insufficient delegation | Required action |
|---|---|---|---|
| YAMLGraph | YAML graph assembly, routing, maps/loops, interrupts, subgraphs, Mermaid | Strict duplicate-key ingress, closed authority vocabulary, provenance/grants/effect policy | Keep a narrow strict front-end; prove and remove duplicated general lowering |
| LangGraph | Supersteps, parallel tasks, retry, subgraphs, Command/interrupt, checkpoints/history | Stable actor/project/effect correlation and irreversible external-send truth | Keep sole workflow owner plus narrow external-effect kernel |
| SqliteSaver | Logical checkpoint lineage and pending writes | Owner grants, external-effect outbox, filesystem mutation | Keep for workflow truth only |
| SQLite | ACID, uniqueness, FK/checks, WAL, writer serialization, CAS rows | Filesystem transaction or external process/send atomicity | Keep semantic tables; audit redundant migration/watch/lock layers only in later separate ranges |
| POSIX `flock` | Crash-released local cross-process exclusion | Content provenance or multi-file atomic transaction | Use one authoring-specific lock in the first range; review global stale-sidecar call sites separately |
| POSIX `openat`/no-follow/rename/fsync | Containment and one-file atomic replacement | Arbitrary multi-directory all-old/all-new transaction | Retain containment; do not build v1 transaction guarantee |
| Git/owner-applied patch | Review, explicit application, familiar rollback | Portable atomic working-tree update without contract change | Available only as explicit B2 product choice; document non-atomic application |
| Provider idempotency | Effect retry where verified provider contract exists | Providers/process launches without exact idempotency/reconciliation | Use opportunistically, never as universal substitute |

The direct LangGraph imports are StateGraph/START/END, Command, MemorySaver, and
SqliteSaver. `langgraph-prebuilt` and SDK are transitive requirements with no
direct Lockstep imports and are not evidence of duplicated Lockstep runtime.

## Complete alternatives

### A. Keep current guarantees

- Retain 39,455 production and 46,627 test Python lines.
- Retain authoring v2–v4 parsing, v4 journal, publication/restoration stages,
  directory ownership, rollback and committed-recovery machines.
- Preserve automatic multi-file all-old/all-new recovery, exact mode/absence
  restoration, and cooperating-writer serialization.
- Migration cost: low. Permanent maintenance/platform proof cost: high.
- Verdict: rejected as the recommendation. Green tests and sunk cost do not
  justify a reliability profile that consumes most authoring code.

### B1. Simplify local-v1 guarantees and preserve automatic writing

- Authoring target: 1,800–2,200 production and 2,300–3,200 test lines in at
  most 10 modules.
- Remove 2,647–3,047 authoring production and 5,643–6,543 test lines; delete
  authoring journal versions and recovery state machines after the v4 cutover
  control below.
- Retain deterministic whole-DAG capture/compile, strict bounds, containment,
  collision preflight, canonical check/diff, and per-file atomic replacement.
- Preserve `recipe init`/template automatic creation of the complete generated
  set. Serialize cooperating Lockstep authoring writers by reusing the current
  logical authoring-writer namespace with one crash-released kernel lock; do not
  create a new lock family. Immediately before **each** replacement,
  validate the exact expected before-image and parent identity using
  descriptor-relative safe opens (`openat`/`O_NOFOLLOW` or an equivalent
  fail-closed mechanism), verify a regular file, and refuse foreign changes.
- After every simulated crash cut, runtime admission—not only optional
  `check`/`diff`—must prove either that every planned generated artifact and the
  exact recipe DAG are canonical and startable, or that the workflow is
  rejected as incomplete/stale and requires explicit regenerate/apply.
- At cutover, a legacy authoring v4 journal must never be ignored: a fixture
  must prove either one-shot recovery before old recovery code is removed or
  explicit fail-closed refusal with actionable old-version recovery guidance.
  The simplified design creates no new journal/version.
- Lose automatic all-old restoration after power/process failure and perfect
  multi-file atomicity. This is explicitly a reliability limitation, not a
  weakening of the selected authority boundary.
- Migration cost: medium and bounded because automatic creation remains the
  installed contract while crash rollback becomes an explicit limitation.
- Verdict: **recommended**.

### B2. Simplify to an owner-applied staged bundle or patch

- Retain the same capture, compilation, containment, collision preflight,
  canonical runtime-admission, and legacy-v4 refusal guarantees as B1.
- Change `recipe init` and template installation to produce a reviewable staged
  bundle/patch instead of mutating generated project files automatically.
- This removes the automatic writer and its cooperating-writer lock, but it is
  a visible CLI/product-contract change and needs separate acceptance fixtures,
  documentation, and migration treatment.
- Target: 1,675–2,050 production lines and 2,100–2,900 test lines in at most
  9 modules. Migration cost: medium-high because owner application becomes a
  required workflow step.
- Verdict: viable only if the user explicitly prefers the changed CLI contract;
  it is not implied by approving B1.

### C. Replace/redesign custom infrastructure broadly

- Potentially retain 20,000–27,000 production and 22,000–31,000 test lines by
  delegating authoring to Git/staging, logical progress to LangGraph, and effect
  retry to verified provider idempotency.
- Could remove 12,000–19,000 production and 15,000–24,000 test lines, including
  parts of effect/watch/publication infrastructure.
- Loses exact external-effect commitment, `indeterminate` truth, independent
  consent correlation, and portable non-duplicate launch guarantees unless
  acceptance goals are materially narrowed.
- Migration cost: high; this is a product redesign, not a refactor.
- Verdict: not recommended now. Reconsider only as an explicit re-scope.

## Causal authoring budget

The current 4,847 production lines are not treated as a single removable blob.
This exact membership makes the B1 cap falsifiable:

| Current responsibility | Exact current module membership | Current physical lines | B1 target |
|---|---|---:|---:|
| Public orchestration/facade | `authoring.py`, `authoring_publisher.py` | 379 | 275–325 |
| Deterministic capture, DAG compile, plan and bounds | `authoring_bundle.py`, `authoring_capture.py`, `authoring_compilation.py`, `authoring_installation.py`, `authoring_limits.py` | 1,096 | 575–650 |
| Canonical observation/diff | `authoring_results.py`, `authoring_observation.py` | 154 | 175–220 |
| Descriptor identity, containment and collision checks | `authoring_identity.py`, `authoring_project_tree.py`, `authoring_file_observation.py` | 899 | 400–450 |
| Transactional publication | `authoring_transaction.py` | 411 | 275–400 for per-file writer and authoring lock |
| Journal/recovery machines | `authoring_stage_paths.py`, `authoring_committed_recovery.py`, `authoring_directory_recovery.py`, `authoring_recovery_observation.py`, `authoring_recovery.py`, `authoring_journal.py`, `authoring_recovery_model.py` | 1,908 | 100–155 for integration and legacy-v4 refusal only |
| **Total** | **20 modules** | **4,847** | **1,800–2,200 in at most 10 modules** |

The production reduction is therefore causal. The journal/recovery surface
falls by 1,753–1,808 lines after retaining 100–155 lines for integration and
legacy-v4 refusal. The remaining 2,939-line
planning/identity/transaction surface falls by 894–1,239 lines without
deleting a retained responsibility. Together those reductions produce the
2,647–3,047 range. B2 replaces the 275–400-line automatic-writer target with a
150–250-line staged-bundle/patch presentation, yielding a 1,675–2,050 target
in at most 9 modules.

The current 8,843 authoring-test lines also have exact membership:

- 2,893 planning/contract/CLI lines in
  `test_authoring_bundle_contracts.py`, `test_authoring_closure_commands.py`,
  `test_authoring_publisher_limits.py`, `test_authoring_bundle_direct_child.py`,
  `test_authoring_planner_capture.py`, `test_authoring_planning_failures.py`,
  `test_authoring_planned_observation.py`,
  `test_authoring_foreign_destination.py`,
  `test_authoring_recipe_init_transaction.py`, and
  `test_template_authoring_transaction.py`;
- 4,813 transaction/recovery/crash-matrix lines in the remaining twelve
  `test_authoring_*` modules whose names contain `recovery`, `transaction`,
  `durability`, or `serialization`;
- 1,137 shared scenario/gate/helper lines in the six `_authoring_*` modules.

B1 allocates 900–1,100 test lines to deterministic/canonical contracts,
500–650 to containment/collision/foreign-modification controls, 450–700 to
automatic-writer crash-cut runtime-admission controls, 300–500 to CLI/template
and runtime-preflight integration, 100–150 to legacy-v4 cutover/refusal, and
50–100 to helpers: 2,300–3,200 total. B2 replaces automatic-writer coverage
with 250–400 staged-bundle/patch lines: 2,100–2,900 total. The follow-on plan
must name the exact retained tests before deleting the current crash matrix;
the numerical cap alone never authorizes test removal.

## Requirement-to-cost decisions

| Unit | Decision |
|---|---|
| Strict bounded ingress, containment, no-follow paths | retain |
| Complete child-first DAG capture and compilation | retain |
| Canonical check/diff and one captured plan | retain |
| Same-directory temp plus per-file atomic replace | retain/simplify |
| Cooperating-writer exclusion | simplify to one authoring-specific kernel lock |
| Multi-file all-old/all-new rollback | defer from v1 |
| Exact bytes/mode/absence/created-directory recovery | defer from v1 |
| Authoring journal v2/v3 compatibility | remove absent real installed evidence |
| Authoring v4 schema and stage/recovery machines | remove only after one-shot recovery or explicit fail-closed legacy refusal fixture |
| Implicit recovery before read-only authoring | replace with stale/incomplete detection and explicit regenerate/apply |
| Exact effect outbox and `indeterminate` | retain |
| LangGraph as sole workflow state/scheduler | retain |
| Automatic background watch recovery | unchanged in first range; audit separately later |
| Publication consent/content/destination/generation binding | retain |
| Runtime multi-file project publication rollback | unchanged in first range; audit separately later |
| 296-line stale sidecar lock | preserve in first range; use a separately budgeted call-site migration later |
| Overlapping blob/artifact/bundle/snapshot stores | unchanged in first range; audit separately and consolidate only where acceptance equivalence is proven |
| Dependency patch | zero growth; delete on fixed upstream release |
| Legacy runner and retired installed contract | remove in Task 12C |
| New providers, schemas, schedulers, cursors, aliases | defer |

## Complexity budgets

### Dedicated simplification range before Task 12C

- No new durable schema or compatibility version.
- No new lifecycle owner, scheduler, manager, or lock family.
- B1 authoring: at most 2,200 production lines, 3,200 test lines, and 10
  modules; remove at least 2,647 production and 5,643 test lines.
- B2 authoring: at most 2,050 production lines, 2,900 test lines, and 9
  modules; remove at least 2,797 production and 5,943 test lines.
- Preserve all non-authoring runtime watch, publication, stale-sidecar lock, and
  effect/authority behavior unchanged. A global lock migration requires its own
  call-site matrix and budget after this range.
- Before each automatic B1 target replacement, revalidate the exact captured
  source closure, expected destination before-image, parent directory identity,
  regular-file shape, and descriptor-relative no-follow containment. Fail
  closed on a foreign/non-regular/unsupported target.
- For every B1 crash cut, black-box runtime admission must accept only the
  complete canonical generated closure and exact DAG; partial output must be
  rejected and reported as stale/incomplete.
- Never silently discard a pre-existing authoring v4 journal. Prove cutover
  recovery before removal or explicit refusal with actionable recovery
  guidance; create no replacement journal.
- Every retained recovery/persistence mechanism must name one accepted
  guarantee and one reachable threat or reliability requirement.
- No opportunistic edits to runtime watch, publication, effect, authority, or
  global locking semantics in this range.

### Task 12C

- Zero new durable schemas.
- Zero new state machines.
- Zero new lifecycle owners or lock families.
- At most 250 gross new production lines and net production growth at or below
  zero after legacy deletion.
- At most 750 new/changed test lines for installed-contract black-box coverage.
- No compatibility aliases, provider abstraction, scheduler, or dependency
  patch growth.

### Post-Task-12 work

- Default net production growth at or below zero until the explicit downstream
  roadmap re-evaluation.
- No new durable schema version unless an existing family is retired 1:1.
- No new lifecycle owner unless another is removed or consolidated 1:1.
- Any custom persistence/recovery mechanism requires evidence that LangGraph,
  SQLite, POSIX, Git, and verified provider idempotency cannot meet the approved
  guarantee.
- Re-audit the 2,811-line lowering, 3,100-line coordinator, 296-line stale
  lock, and 1,808-line overlapping content stores by requirement before adding
  features. They are candidates, not pre-approved deletion ranges.

## Known evidence gaps and decision effect

- No usage telemetry or incident count proves real demand for concurrent
  authoring writers, power-loss recovery, or exact mode/absence restoration.
  Absence of evidence weighs against making these v1 guarantees mandatory.
- No real installation inventory demonstrates v2/v3 authoring journals, while
  a development install can have the currently emitted v4 journal. Historical
  compatibility must not survive on a hypothetical population, but v4 must be
  recovered before cutover or refused explicitly rather than ignored.
- The current implementation and environment are POSIX/macOS. A broader
  platform promise is not frozen and cannot justify the stale sidecar protocol.
- Dependency upstream test LOC is unavailable from installed wheels and is not
  used in the recommendation.
- Runtime latency/storage overhead has not been benchmarked. The simplification
  case rests on structural ownership and maintenance cost, not an invented
  performance claim.
- Exact compiler/coordinator/CAS reductions need causal acceptance fixtures in
  later dedicated audits; this Gate does not authorize their deletion.

These gaps do not block the recommended authoring simplification. They do block
claims that the broader compiler, effect coordinator, or CAS candidates can be
removed without their own bounded plans and RED evidence.

## Required explicit decision

After independent product-scope/proportionality, architecture/SRP, and
threat-model reviews, the user must select exactly one:

1. `keep` — retain the current architecture and accept its measured cost;
2. `simplify-with-write` — preserve automatic CLI/template creation while
   removing multi-file rollback/recovery (**recommended**);
3. `simplify-owner-applied-patch` — replace automatic project mutation with a
   staged bundle/patch and make owner application part of the product contract;
4. `redesign/re-scope` — change broader product/authority guarantees;
5. `stop` — end the project here.

This document is read-only analysis. It authorizes no production change by
itself.
