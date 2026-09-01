# Lockstep Native yamlgraph/LangGraph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking. Every task gets a fresh
> implementation subagent and a separate reviewer; findings are fixed and
> re-reviewed to zero before the next task.

**Goal:** Complete the Lockstep DSL and runtime while yamlgraph/LangGraph remain
the only workflow engine and Lockstep durably coordinates only external effects.

**Architecture:** DSL compiles deterministically to checked-in yamlgraph YAML;
yamlgraph compiles native LangGraph topology against a native SQLite saver. A
thin GraphRuntime projects public snapshots/history and resumes protected typed
interrupts. SQLAlchemy-backed sidecars contain immutable run discovery and
coordinate-bound external-effect facts only.

**Tech Stack:** Python 3.11+, yamlgraph with the reviewed native-subgraph patch,
LangGraph 1.2.10, langgraph-checkpoint-sqlite, SQLAlchemy Core, PyYAML,
jsonschema, pytest, Ruff, Codex CLI/macOS acceptance.

**Specs:**

- `.superpowers/handover/2026-08-20-lockstep-langgraph-native-handover.md`
  (historical transition and document-authority record)
- `.superpowers/specs/2026-08-19-lockstep-workflow-dsl-design.md` (authoritative
  only for language, authoring/static semantics, and completed Tasks 1–5; its
  later custom-runtime architecture is rejected)
- `.superpowers/specs/2026-08-20-lockstep-langgraph-native-design.md`
- `.superpowers/specs/2026-08-20-lockstep-threat-model.md` (normative security
  boundary and review policy)

The native design plus threat model govern runtime architecture and review. The
handover resolves conflicts with the older DSL design. Rejected execution-kernel
specifications and the old 17-task plan are historical only and cannot create
requirements or downstream task numbers.

## Global constraints

- Work only in
  `<HOME>/Projects/pets/claude-essentials-worktrees/lockstep-workflow-dsl/lockstep`
  on
  `feat/lockstep-workflow-dsl`; never merge to `main` in this plan.
- Preserve completed Tasks 1–5 unless a failing native integration test proves a
  concrete conflict.
- Production topology is always yamlgraph YAML -> `compile_graph` -> LangGraph.
- Only `src/lockstep/recipe/yamlgraph_adapter.py` imports yamlgraph/LangGraph.
- Never add KernelStore, WorkItem, branch/join tables, checkpoint copies, private
  saver-table reads, custom resume tokens, runtime handler monkeypatches, vendored
  yamlgraph, or a local path dependency.
- The sole temporary yamlgraph modification is the reviewed build-time patch in
  Task 1, applied fail-closed to the exact official locked distribution.
- Public status is exactly `starting|awaiting|running|completed|escalated|aborted`.
- Public submission remains `scenario_done`; session binding is mandatory for a
  returned worker-owned interrupt.
- Generated terminal state uses
  `lockstep_outcome=PASS|FAIL|ERROR|ABORTED`.
- Declared writes use `ProjectWritePath`: exact file or directory prefix ending
  `/`; no write globs.
- Every external attempt is keyed by thread/checkpoint namespace/checkpoint/task/
  interrupt/descriptor digest and never duplicated after ambiguous launch.
- Use TDD: commit RED tests separately from GREEN implementation where the
  upstream/repository hook permits; always preserve command evidence.
- Each task ends with focused tests, full proportional regression, independent
  spec review, independent quality review, finding fixes, re-review to zero, and
  a Conventional Commit.
- Security findings MUST use the threat model's finding format and identify the
  attacker's pre-existing authority, the authority gained, a non-zero authority
  delta, the reachable trust-boundary path, and the deployment profile. A bypass
  requiring arbitrary code execution, reflection, or debugger access inside the
  TCB is not a security finding unless an untrusted party actually receives the
  relevant internal reference.
- Apply the threat model's stop rule at every review. Once an invariant is enforced
  at the earliest common boundary, do not recursively harden trusted internal
  representations. Classify remaining work honestly as correctness, reliability,
  or optional defense-in-depth; remove code that has no justified role in one of
  those classes.

### Review finding admission filter

Every review, including RED-test, GREEN, architecture, reliability, concurrency,
and security review, MUST apply this filter before treating an observation as an
actionable finding:

1. Name the exact violated invariant and its normative source in the threat
   model, approved plan, frozen contract, or existing supported behavior.
2. Demonstrate a reachable scenario inside the selected deployment profile; a
   merely imaginable state or an excluded actor is insufficient.
3. State the user-visible consequence: authority expansion, data loss, mixed or
   falsely committed state, indefinite operational blockage, or another concrete
   correctness/reliability failure.
4. Show that the scenario represents a new durable state or consequence not
   already covered by an existing equivalence class. More instruction offsets,
   byte percentages, ordinals, payload spellings, or equivalent interleavings do
   not create new requirements.
5. For a security classification, additionally prove the threat model's non-zero
   authority delta and reachable trust-boundary path. Otherwise classify the
   observation as correctness, reliability, or optional defense-in-depth.
6. Show that the proposed remediation is proportionate to the consequence and
   does not expand the task beyond its approved boundary.

Only admitted `Critical` and `Important` findings block the next planned
milestone. A `Minor` may be fixed immediately only when it exposes a false test
oracle, architectural decomposition/SRP violation, or a genuinely local near-zero-risk
defect; otherwise record its disposition in the backlog and continue. Optional
defense-in-depth does not block MVP work.

For fault and concurrency work, freeze the relevant state/equivalence matrix
before implementation. Once every cell in that matrix is green and no admitted
blocking finding remains, the block is closed. Reopening or expanding it requires
evidence of a new invariant, reachable durable state, or user-visible consequence;
reviewer preference or "what if" alone is not sufficient. "Re-review to zero"
means zero admitted blocking findings, with every non-blocking observation given
an explicit disposition; it does not mean implementing every suggestion.

## Target file map

```text
engine/src/lockstep/recipe/yamlgraph_adapter.py   sole native-library boundary
engine/src/lockstep/runtime/native_models.py      native-neutral DTOs/results
engine/src/lockstep/runtime/recipe_bundles.py     immutable recipe DAG bundles
engine/src/lockstep/runtime/storage.py            SQLAlchemy tables/transactions
engine/src/lockstep/runtime/catalog.py            immutable public run bindings
engine/src/lockstep/runtime/leases.py             invocation/effect/session leases
engine/src/lockstep/runtime/graph_runtime.py       thin start/snapshot/history/resume
engine/src/lockstep/runtime/status.py              public status/owner projection
engine/src/lockstep/runtime/effects/models.py      descriptor/result/attempt types
engine/src/lockstep/runtime/effects/descriptors.py closed payload validation/digest
engine/src/lockstep/runtime/effects/ledger.py      external-attempt facts only
engine/src/lockstep/runtime/effects/coordinator.py crash reconciliation/delivery
engine/src/lockstep/runtime/providers/base.py      neutral provider protocols
engine/src/lockstep/runtime/providers/codex.py     Codex-only mechanics
engine/src/lockstep/runtime/providers/manual.py    session handoff adapter
engine/src/lockstep/runtime/providers/pinned.py    pinned-command adapter
engine/src/lockstep/runtime/blobs.py               content-addressed bytes
engine/src/lockstep/runtime/artifacts.py           provenance registry
engine/src/lockstep/runtime/project_snapshots.py   provider-neutral snapshots
engine/src/lockstep/runtime/publication.py         recoverable project publication
engine/src/lockstep/runtime/events.py              observational event projection
engine/src/lockstep/runtime/service.py             public scenario application API
engine/src/lockstep/workflow/compiler.py           IR -> yamlgraph entry point
engine/src/lockstep/workflow/lowering.py           deterministic graph fragments
engine/src/lockstep/workflow/canonical.py          canonical YAML/source maps
engine/src/lockstep/workflow/freshness.py          dependency DAG verification
```

---

### Task 1: Apply the reviewed yamlgraph fix during installation; prove native capabilities

**Files:**

- Use: `.superpowers/upstream/yamlgraph-subgraph-config.patch`
- Use: `.superpowers/upstream/yamlgraph-subgraph-config-issue-draft.md`
- Modify: `engine/pyproject.toml`
- Modify: `engine/uv.lock`
- Create: `engine/src/lockstep/_dependency_patches/yamlgraph/0.5.22-subgraph-config.patch`
- Create: `engine/src/lockstep/_dependency_patches/yamlgraph/manifest.json`
- Create: `engine/src/lockstep/dependency_patch.py`
- Create: `engine/src/lockstep/bootstrap.py`
- Create: `engine/src/lockstep/__main__.py`
- Create: `engine/scripts/apply_dependency_patches.py`
- Create: `engine/scripts/probe_yamlgraph_native.py`
- Create: `scripts/lockstep-install`
- Create: `scripts/lockstep-build`
- Modify: `scripts/lockstep-plugin`
- Modify: `engine/src/lockstep/runtime/engine.py`
- Modify: `README.md`, `docs/`
- Modify: `engine/src/lockstep/recipe/yamlgraph_adapter.py`
- Create: `engine/src/lockstep/runtime/native_models.py`
- Create: `engine/tests/test_dependency_patches.py`
- Modify: `engine/tests/test_plugin_packaging.py`
- Modify: `engine/tests/test_engine_subcalls.py`
- Create: `engine/tests/recipe/test_native_capabilities.py`
- Create: `engine/tests/fixtures/native/child_interrupt.recipe.yaml`
- Create: `engine/tests/fixtures/native/parent_direct.recipe.yaml`
- Create: `engine/tests/fixtures/native/parallel_interrupts.recipe.yaml`

**Interfaces:**

- Produces `NativeApp` in `yamlgraph_adapter.py` with `invoke`, `ainvoke`, `stream`,
  `snapshot`, `history`, and `close`; no native object crosses the adapter.
- Produces native-neutral DTOs in `runtime/native_models.py`; that module has no
  yamlgraph/LangGraph imports.
- Keeps an official immutable PyPI pin and applies one source-only patch during
  the canonical dependency-install/build flow until an upstream release passes
  the same capability gate.
- Packages `lockstep-dependency-install` as the explicit applicator entry point;
  ordinary `lockstep` and `python -m lockstep` remain read-only verifiers.

- [x] **Step 1: Verify and record the already-approved upstream issue**

Keep the full upstream-quality patch and test evidence in `.superpowers/upstream/`.
The owner approved publication; issue
`https://github.com/sheikkinen/yamlgraph/issues/474` and patch comment
`https://github.com/sheikkinen/yamlgraph/issues/474#issuecomment-5354688575`
already contain the agreed description and exact tested artifact. Verify these
read-only and record their URLs plus canonical body/patch digests in the report
and manifest. Do not create a duplicate issue, fork, branch, PR, gist, or attachment.

- [x] **Step 2: Write native adapter capability tests**

Tests must call only `lockstep.recipe.yamlgraph_adapter`, never import LangGraph.
The restart witness has this shape:

```python
def test_direct_child_interrupt_survives_sqlite_restart(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    first = yg.open_native_app(PARENT_DIRECT, db)
    parked = first.invoke({}, thread_id="parent-a")
    coordinate = parked.pending[0].coordinate
    first.close()

    restarted = yg.open_native_app(PARENT_DIRECT, db)
    completed = restarted.resume(
        thread_id="parent-a",
        results_by_interrupt_id={coordinate.interrupt_id: "yes"},
    )
    assert completed.values["answer"] == "yes"
    assert completed.pending == ()
```

Add tests for two parallel interrupts, partial resume, batch resume, native join,
cycle/loop limit, reducer aggregation, a real `NativeApp.ainvoke` direct smoke,
subgraph snapshot, and two invoke parents with isolated child identities. Add
three independent witnesses: (1) a YAML direct child proving native interrupt,
SQLite restart/resume, child spans, and no synthetic outer OTel span; (2) YAML
invoke/relay parents proving thread isolation and filtering of executor-private
config; (3) an adapter-owned minimal real `StateGraph` whose config-aware callable
is composed with the actual yamlgraph timeout then OTel wrappers and writes a
unique injected configurable sentinel into graph state, with OTel enabled and
disabled. The third witness is capability-probe code only: no production topology
shim, DSL feature, coordinate reconstruction, or new public workflow API.

- [x] **Step 3: Run RED against released yamlgraph 0.5.22**

Create an isolated temporary uv environment, install exactly `yamlgraph==0.5.22`
plus the engine's lock-exported test/runtime dependencies, assert
`importlib.metadata.version("yamlgraph") == "0.5.22"`, install Lockstep editable
with dependency resolution disabled, and run:

```bash
cd engine
uv venv /private/tmp/lockstep-yamlgraph-0522-red
uv export --frozen --all-groups --no-emit-project \
  --no-emit-package yamlgraph \
  -o /private/tmp/lockstep-native-capability-requirements.txt
uv pip install --python /private/tmp/lockstep-yamlgraph-0522-red/bin/python \
  -r /private/tmp/lockstep-native-capability-requirements.txt
uv pip install --python /private/tmp/lockstep-yamlgraph-0522-red/bin/python \
  'yamlgraph==0.5.22'
uv pip install --python /private/tmp/lockstep-yamlgraph-0522-red/bin/python \
  --no-deps -e .
/private/tmp/lockstep-yamlgraph-0522-red/bin/python -c \
  'import importlib.metadata as m; assert m.version("yamlgraph") == "0.5.22"'
/private/tmp/lockstep-yamlgraph-0522-red/bin/python -m pytest \
  tests/recipe/test_native_capabilities.py -q
```

Expected: direct composition/config-isolation tests fail on unpatched 0.5.22.

- [x] **Step 4: Write RED install/build patch-state tests**

Test the real scripts against isolated copied distributions. Name the production
break each test catches and cover: exact-original application; after-hash
verification; idempotent fully-patched rerun; patch digest mismatch; unknown,
mixed, partial, and wrong-version refusal; a newer unpatched distribution whose
probe passes returning `patch obsolete`; a newer distribution whose probe fails
returning `patch diverged`; paths containing spaces; read-only/error cleanup; and
no modification outside the located distribution. Exercise `lockstep-install`,
`lockstep-build`, and `lockstep-plugin` with a fake `uv`/application boundary and
assert sync -> patch -> `--no-sync` execution order.
Use a genuine temporary non-Git site-packages tree. Test the packaged console
entry point and `python -m lockstep` against original, fully patched, mixed,
unknown, and wrong-version distributions; only fully patched state may lazy-import
CLI. Black-box `uv run --project ... lockstep` must also fail closed when sync has
restored original or mixed files.
Build the Lockstep wheel into a clean venv: `lockstep` must fail before patching,
the installed `lockstep-dependency-install` command must patch successfully and
idempotently, and only then may the same installed `lockstep` start.

```bash
cd engine
uv run pytest tests/test_dependency_patches.py tests/test_plugin_packaging.py -q
```

Expected: fail because the canonical installer, manifest, and script sequencing
do not yet exist.

- [x] **Step 5: Implement the ordinary install/build patch step and adapter seam**

Update the official dependency to `yamlgraph==0.5.22` and lock its PyPI wheel/
sdist hashes. Derive a minimal patch containing only the four changed yamlgraph
source files; do not ship upstream tests/docs or copied modules. Manifest fields
are closed and include schema, distribution, exact version, upstream repository,
exact upstream issue and patch-comment URLs, full patch SHA-256, and sorted
`{path, before_sha256, after_sha256}` entries.

The standard-library applicator locates yamlgraph through distribution metadata,
validates that the diff names exactly the manifest's safe contained relative paths,
and checks every hash. Stage a copy in a temporary directory that is not a Git
worktree, remove every `GIT_*` variable from the subprocess environment, and run
`git apply --no-index --check <patch>` then `git apply --no-index <patch>` with an
argv array and staged-distribution cwd; never use `--unsafe-paths`. It never imports
Lockstep runtime or yamlgraph before deciding patch state. Verify all staged output
hashes, then atomically replace only the touched installed files. On failure retain
diagnostics, restore verified originals if replacement began, and never leave a
mixed state.

`scripts/lockstep-install` performs locked sync then invokes
`uv run --no-sync lockstep-dependency-install`; `scripts/lockstep-build` calls
install before `uv build`; the
plugin launcher calls install before `uv run --no-sync lockstep`. Update adapter
version documentation, add `NativeApp`, convert native values to neutral DTOs,
call `SqliteSaver.setup()`, and close owned SQLite connections deterministically.
Point `[project.scripts].lockstep` to a minimal bootstrap that read-only verifies
the fully patched state before lazy-importing `lockstep.cli`; `python -m lockstep`
uses the same bootstrap. It never applies at runtime. Migrate generated child
launchers and documented dev commands to the canonical installer/`--no-sync`
path; bypass attempts still fail at bootstrap.
Add `[project.scripts].lockstep-dependency-install` pointing directly to the pure
applicator CLI, not to bootstrap, so a built-wheel installation has the same
explicit dependency-install path as the source tree.

- [x] **Step 6: Verify GREEN and upstream-change detection**

```bash
cd engine
uv lock
cd ..
scripts/lockstep-install
cd engine
uv run --no-sync pytest tests/test_dependency_patches.py tests/test_plugin_packaging.py tests/recipe/test_native_capabilities.py tests/recipe/test_adapter_boundary.py -q
uv run --no-sync pytest -q
```

Also install an isolated clean official `0.5.22` to prove RED before patch, apply
the canonical installer to prove GREEN, and run simulated newer-package obsolete/
diverged cases without network access.

- [x] **Step 7: Commit and review**

Commit `test(runtime): gate native yamlgraph subgraph capabilities`, dispatch the
two required independent reviews, fix every finding, rerun Step 6, and commit
review fixes before Task 2.

---

### Task 2: Immutable bundles, snapshots, RunCatalog, SQL schema, and leases

**Files:**

- Modify: `engine/pyproject.toml`, `engine/uv.lock`
- Create: `engine/src/lockstep/runtime/recipe_bundles.py`
- Create: `engine/src/lockstep/runtime/storage.py`
- Create: `engine/src/lockstep/runtime/catalog.py`
- Create: `engine/src/lockstep/runtime/leases.py`
- Create: `engine/src/lockstep/runtime/blobs.py`
- Create: `engine/src/lockstep/runtime/project_snapshots.py`
- Create: `engine/tests/runtime/test_recipe_bundles.py`
- Create: `engine/tests/runtime/test_catalog.py`
- Create: `engine/tests/runtime/test_leases.py`
- Create: `engine/tests/runtime/test_blobs.py`
- Create: `engine/tests/runtime/test_project_snapshots.py`

**Interfaces:**

- `RecipeBundleStore.capture(project_root, validated_dag) -> RecipeBundleRef`
- `RecipeBundleStore.materialize_for_compile(ref) -> MaterializedRecipe`
- `RunCatalog.create(binding)`, `get(run_id)`, `list(project_identity)`; no update
  method exists.
- `LeaseStore.acquire(scope, key, owner, ttl) -> Lease`; epochs fence stale owners.
- `BlobStore.put(bytes) -> BlobRef` is SHA-256 addressed and immutable.
- `ProjectSnapshotStore.capture(...) -> ProjectSnapshotRef` records a sealed,
  provider-neutral rollover manifest over BlobRefs.

- [ ] **Step 1: Add SQLAlchemy Core and failing schema/domain tests**

Add `sqlalchemy>=2.0,<3`. Assert exact immutable catalog columns and absence of
`status`, `step`, `brief`, `branch`, `terminal`, or checkpoint blobs:

```python
def test_run_catalog_has_no_workflow_state(sqlite_store):
    assert set(sqlite_store.tables.runs.c.keys()) == {
        "public_run_id", "thread_id", "recipe_digest",
        "recipe_snapshot_ref", "project_identity", "created_at",
    }
```

Bundle/snapshot tests include absolute/traversal/symlink/duplicate rejection, deterministic
manifest ordering, digest mismatch, concurrent capture, and original parent/child
mutation/deletion after capture. Blob tests cover immutable duplicate writes and
digest mismatch; snapshot tests cover declared paths, provenance, and sealed reuse.

- [ ] **Step 2: Verify RED**

```bash
cd engine
uv run pytest tests/runtime/test_recipe_bundles.py tests/runtime/test_catalog.py tests/runtime/test_leases.py tests/runtime/test_blobs.py tests/runtime/test_project_snapshots.py -q
```

- [ ] **Step 3: Implement focused stores**

Use SQLAlchemy tables only inside `storage.py`; use transactions and unique
constraints for immutable bindings. Bundle manifests contain root relative path
and ordered `{path, sha256, size}` entries. Materialization verifies all bytes,
uses an owner-state directory, rejects symlinks, writes atomically, and makes the
tree read-only before returning its root recipe path.

Lease scopes are exactly `invoke`, `effect`, `session`, and `publication`.
Expiry permits a higher epoch owner but never implies permission to relaunch.
Blob and project-snapshot stores are generic immutable persistence primitives;
they contain no effect, artifact, workflow, route, or publication decisions.

- [ ] **Step 4: Verify GREEN and full persistence regression**

```bash
cd engine
uv lock
uv run pytest tests/runtime/test_recipe_bundles.py tests/runtime/test_catalog.py tests/runtime/test_leases.py tests/runtime/test_blobs.py tests/runtime/test_project_snapshots.py -q
uv run pytest tests/test_runs.py tests/test_locking.py tests/test_session_binding.py -q
```

- [ ] **Step 5: Commit and review**

Commit `feat(runtime): add immutable run and recipe binding`, review to zero, fix,
rerun Step 4, and commit fixes.

---

### Task 2A: Close reachable MVP boundaries identified by the threat-model audit

This task is a release gate, not optional defense-in-depth. It applies the
normative threat model at the earliest shared boundaries before GraphRuntime is
wired into public commands.

**Storage and bundle closure:**

- Lease release MUST preserve a monotonic fencing generation. Reacquiring the
  same `(scope, key)` can never recreate an earlier token, including ABA with the
  same owner.
- A common state-root initializer MUST create and verify owner-only directories,
  SQLite files and sidecars, manifests, blobs, snapshots, and materializations.
  Insecure ownership or modes fail closed before use.
- Blob, bundle, snapshot, manifest, dependency-count, and provenance inputs MUST
  have explicit admission limits. Limit failure publishes no authoritative
  manifest or catalog binding; orphan content is either prevented by preflight or
  safely collectible.
- Recipe capture MUST walk every path from a held project-root directory handle,
  reject links/special files at every component, and close check/use races with
  descriptor-relative opens on supported platforms.
- Bundle capture accepts only a typed `ValidatedDependencyDAG` containing the root
  and exact ordered files, and descriptor-safely captures exactly those files.
  Storage does not interpret recipe/yamlgraph language semantics. The following
  Recipe Authority work remains the A-07 release gate: its single strict loader
  must produce the closed DAG, and its compile adapter must resolve only within
  the immutable materialization; undeclared, absolute, live-project, and foreign
  paths fail closed there.

**Recipe authority closure:**

- One strict loader rejects duplicate keys, aliases, ambiguous scalar coercions,
  excessive size/depth/cardinality, and unknown authority-bearing fields before
  yamlgraph sees the recipe.
- Until the typed effect bridge and runtime grant gate exist, public start MUST
  fail closed for shell/tool nodes and arbitrary Python module/function selection.
  Later manual-yamlgraph parity admits only the closed protected descriptors
  defined by this design; edit authority never implies `os_user_execution`.
- RED/GREEN tests observe both the rejection and absence of compile, process
  launch, catalog mutation, or checkpoint creation.

**Explicit audit dispositions:**

- Do not harden internal frozen mappings against `object.__setattr__`, reflection,
  debugger, `ctypes`, or arbitrary code inside the TCB. No untrusted reference is
  exposed; the threat-model stop rule applies.
- Do not add a cryptographic executable pin or PATH scanner to the local
  owner-controlled installer. Such a control becomes deployment-required only if
  a less-trusted actor can choose the build environment or executable resolver.
- Do not extend the legacy Engine to repair its connection lifecycle. Task 3's
  atomic native cutover removes that ownership path; add a regression proving the
  new owner closes NativeApp resources.

Run focused boundary tests, the full Task 1–2 suite, and an independent
threat-model conformance review to ZERO before Task 3.

---

### Task 3: Thin GraphRuntime and native public status projection

**Files:**

- Create: `engine/src/lockstep/runtime/graph_runtime.py`
- Create: `engine/src/lockstep/runtime/status.py`
- Modify: `engine/src/lockstep/recipe/yamlgraph_adapter.py`
- Create: `engine/src/lockstep/runtime/service.py`
- Delete: `engine/src/lockstep/runtime/runs.py`
- Replace: `engine/src/lockstep/runtime/engine.py` with a state-free delegating facade
- Modify: `engine/src/lockstep/cli.py`, `engine/src/lockstep/mcp/server.py`
- Modify: `engine/src/lockstep/runtime/hooks.py`
- Modify: `engine/src/lockstep/runtime/config.py`
- Create: `engine/tests/runtime/test_graph_runtime.py`
- Create: `engine/tests/runtime/test_native_status.py`
- Modify: `engine/tests/test_hooks_cli.py`, `engine/tests/test_session_binding.py`
- Modify: `engine/tests/test_engine.py`, `engine/tests/test_server.py`
- Delete: `engine/tests/test_runs.py` (superseded by catalog/status/runtime tests)
- Delete: `engine/tests/_subcall_helpers.py`
- Delete: `engine/tests/test_engine_subcalls.py`
- Delete: `engine/tests/test_integration_subcalls.py`
- Delete: `engine/tests/test_daily_change_recipe.py` (recreated natively in Task 9)
- Modify: `engine/tests/test_integration.py`
- Modify: `engine/tests/test_origin_binding.py`
- Modify: `engine/tests/test_runners.py`
- Create: `engine/tests/runtime/test_hook_projection.py`
- Create: `engine/tests/runtime/test_no_legacy_workflow_state.py`
- Create: `engine/tests/runtime/test_legacy_test_migration.py`

**Interfaces:**

- Exact GraphRuntime methods are `bind`, `start`, `snapshot`, `history`, `resume`,
  and `stream` from the spec.
- `project_status(binding, snapshot, leases, effects) -> ScenarioStatus` returns
  only the six public status values and owner-safe annotations.

- [ ] **Step 1: Write failing runtime tests**

Cover bind from immutable materialization, fresh start, restart, history,
subgraphs, stale checkpoint rejection, wrong task/interrupt rejection, individual
and batch resume, invocation lease fencing, connection close, and live source
deletion. Include:

```python
def test_status_is_derived_not_catalogued(runtime, catalog, parked_run):
    snapshot = runtime.snapshot(parked_run.public_run_id)
    status = project_status(parked_run.binding, snapshot, (), ())
    assert status.status == "awaiting"
    assert "status" not in catalog.columns
```

Status tests map worker -> awaiting, active invoke/engine/child/effect -> running,
PASS -> completed, FAIL/ERROR/native task error -> escalated, ABORTED -> aborted,
and binding-before-first-visible-checkpoint -> starting.

Add an explicit legacy-test disposition matrix. `test_runs.py` persistence/status
cases map to RunCatalog, GraphRuntime, native status, and lease tests. Root origin
binding maps to session/native-coordinate tests; native children assert no public
run or credential. Daily/integration coverage uses Task 1 native child fixtures;
old subprocess-child lifecycle/budget/credential cases are deleted as forbidden
architecture and map to Task 5 crash tests plus Task 9 native child/restart tests.
`test_runners.py` keeps parser/validation coverage but drops Engine construction.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/test_graph_runtime.py tests/runtime/test_native_status.py tests/runtime/test_no_legacy_workflow_state.py tests/runtime/test_hook_projection.py tests/runtime/test_legacy_test_migration.py tests/test_hooks_cli.py tests/test_session_binding.py tests/test_engine.py tests/test_server.py tests/test_integration.py tests/test_origin_binding.py tests/test_runners.py -q
```

- [ ] **Step 3: Implement without native imports**

GraphRuntime depends on adapter protocols and DTOs only. It verifies source
lineage through adapter `history`, verifies current pending membership, and passes
the interrupt-ID map to the adapter. Status reads snapshots/tasks and lease/effect
annotations but performs no mutation.

In the same atomic cutover, production construction switches to
GraphRuntime+RunCatalog. `LockstepService` exposes start/status/history plus generic
`scenario_done`/`scenario_escalate`/`scenario_abort`: each validates the current
native pending worker coordinate and session binding, then resumes it with the
closed control/result payload; it never writes a public status. Provider-specific
manual/pinned behavior remains Task 7.
The compatibility-named `Engine` delegates to it and owns no transitions or
persistence. Delete `runs.py`, ban production reads/writes of `runs.json`, and
wire CLI/MCP constructors to the new service now. Add an import/AST architecture
test that rejects new `RunIndex`, `ACTIVE_STATUS`, runs.json mutation, or legacy
Engine transition calls. Later tasks may extend the service but cannot revive a
second workflow engine.

Migrate hooks and doctor in the same cutover: they read RunCatalog plus native
projected status through a read-only service interface, retain policy/session
ownership gates, never advance a graph, and report redacted binding-integrity
failures. Remove child-run ancestry and child credentials because native children
have no public run identity. `config.py` no longer exposes a runs.json path.

- [ ] **Step 4: Verify and commit**

```bash
cd engine
uv run pytest tests/runtime/test_graph_runtime.py tests/runtime/test_native_status.py tests/runtime/test_no_legacy_workflow_state.py tests/runtime/test_hook_projection.py tests/recipe/test_native_capabilities.py tests/test_hooks_cli.py tests/test_session_binding.py tests/test_engine.py tests/test_cli.py tests/test_server.py -q
uv run pytest -q
```

Commit `feat(runtime): project runs from native checkpoints`, review/fix/re-review
to zero, and rerun the command.

---

### Task 4: Protected descriptors and coordinate-bound EffectLedger

**Files:**

- Create: `engine/src/lockstep/runtime/effects/__init__.py`
- Create: `engine/src/lockstep/runtime/effects/models.py`
- Create: `engine/src/lockstep/runtime/effects/descriptors.py`
- Create: `engine/src/lockstep/runtime/effects/ledger.py`
- Modify: `engine/src/lockstep/runtime/storage.py`
- Create: `engine/tests/runtime/effects/test_descriptors.py`
- Create: `engine/tests/runtime/effects/test_ledger.py`

**Interfaces:**

- `parse_effect_descriptor(value) -> EffectDescriptor`
- `derive_effect_id(coordinate, descriptor_digest) -> EffectId`
- `EffectLedger.prepare`, `mark_launching`, `mark_running`, `seal`,
  `mark_indeterminate`, `mark_delivered`, and read-only queries.

- [ ] **Step 1: Write closed-schema and identity RED tests**

Reject unknown version/kind/key, callable/argv injection, unsafe writes, unknown
state selectors, oversized values, descriptor digest mismatch, duplicate
coordinate with different descriptor, illegal phase edges, stale revisions, and
same effect/different runner binding. Cover closed call/parallel ScopeResult PASS/
ERROR variants, null/bounded deadlines, min-of-ancestors calculation, runner/scope
digest binding, direct `prepared -> sealed -> delivered`, and expired no-spawn.
Reject a deadline/bounded-scope reference on an unmanaged manual descriptor.
Assert the only ambiguity result:

```python
record = ledger.mark_indeterminate(effect_id, expected_revision=2)
assert record.phase == "indeterminate"
assert record.result.outcome == "ERROR"
assert record.result.fixed_error_code == "launch_indeterminate"
```

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/effects/test_descriptors.py tests/runtime/effects/test_ledger.py -q
```

- [ ] **Step 3: Implement external facts only**

Canonical JSON uses sorted keys, declaration-ordered arrays, UTF-8, and no
insignificant whitespace. Effect table contains exactly the spec fields plus a
CAS revision. An append-only external observation table may record phase/reason
history but contains no node, route, join, or workflow status authority.

- [ ] **Step 4: Run architecture guard and GREEN**

```bash
cd engine
uv run pytest tests/runtime/effects/test_descriptors.py tests/runtime/effects/test_ledger.py -q
uv run pytest tests/runtime/test_no_legacy_workflow_state.py -q
```

The architecture test must find no new forbidden production architecture. Commit
`feat(runtime): persist coordinate-bound effect facts`, then review/fix to zero.

---

### Task 5: Provider protocols and crash-safe effect coordinator with fakes

**Files:**

- Create: `engine/src/lockstep/runtime/providers/__init__.py`
- Create: `engine/src/lockstep/runtime/providers/base.py`
- Create: `engine/src/lockstep/runtime/effects/coordinator.py`
- Create: `engine/tests/runtime/providers/fakes.py`
- Create: `engine/tests/runtime/effects/test_coordinator.py`
- Create: `engine/tests/runtime/effects/test_crash_matrix.py`

**Interfaces:**

- RunnerAdapter methods: `prepare`, `ensure_started`, `inspect`, `cancel`,
  `quiesce`.
- `EffectCoordinator.reconcile(run_id) -> ReconcileReport`
- `EffectCoordinator.deliver_ready(run_id, interrupt_ids=None) -> ScenarioStatus`
- `EffectCoordinator.reconcile_due(now) -> tuple[ReconcileReport, ...]`; injected
  clock/wakeup makes the nearest-deadline loop deterministic.

- [ ] **Step 1: Encode the complete crash matrix as RED tests**

Create deterministic fakes recording every port call. Cover parked/no-row,
prepared/no-spawn, launching absent/adopted/indeterminate, running/deadline,
terminal-safety pending/proven, managed rollover required, sealed/pending resume,
post-commit/pre-delivered, incompatible lineage, pre-resume artifacts, concurrent
reconcilers, partial/batch delivery, nearest-deadline wakeup, overdue startup
scan, scope deadline inheritance, timeout quiescence, and already-expired no-spawn.
Assert no second launch:

```python
coordinator.reconcile(run_id)
coordinator.reconcile(run_id)
assert fake_runner.ensure_started_calls == 1
```

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/effects/test_coordinator.py tests/runtime/effects/test_crash_matrix.py -q
```

- [ ] **Step 3: Implement one monotonic reconciliation decision per call**

Read native snapshot first; acquire effect lease; validate coordinate; perform at
most one irreversible boundary; CAS the observation; release. `inspect`, `cancel`,
and `quiesce` never call `ensure_started`. Seal only after matching terminal-safety
and rollover/stability proof. Deliver stored results through GraphRuntime only.
The wakeup loop queries nonterminal effect deadlines, sleeps to the nearest with a
one-second maximum recheck, and calls the same idempotent reconciliation; it owns
no timer/workflow table and startup uses the same overdue scan.

- [ ] **Step 4: Verify GREEN, race tests, and commit**

```bash
cd engine
uv run pytest tests/runtime/effects -q
uv run pytest tests/runtime/test_graph_runtime.py tests/runtime/test_leases.py -q
```

Commit `feat(runtime): reconcile external effects at native interrupts`; complete
both reviews and fixes before Task 6.

---

### Task 6: Real Codex managed-effect vertical

**Files:**

- Create: `engine/src/lockstep/runtime/providers/codex.py`
- Create: `engine/src/lockstep/runtime/providers/workspaces.py`
- Modify: `engine/src/lockstep/runtime/runners.py`
- Modify: `engine/src/lockstep/runtime/sandbox.py`
- Modify: `engine/src/lockstep/runtime/manifests.py`
- Create: `engine/tests/runtime/providers/test_codex.py`
- Create: `engine/tests/runtime/providers/test_provider_contract.py`
- Create: `engine/tests/integration/test_managed_effect.py`

**Interfaces:**

- `CodexRunnerAdapter` implements RunnerAdapter without provider data leaking into
  EffectRequest/EffectResult.
- `LocalGitWorkspaceProvider` implements materialize, rollover, quarantine,
  release, and inspection.

- [ ] **Step 1: Write provider contract RED tests**

Verify request digest binding, exact argv arrays without shell, owner-captured
permission profile, sanitized environment, deadline recheck, launcher decision
fence, lookup-before-start, adopt-not-duplicate, sandbox attestation, symlink/VCS/
outside-root denial, quiescence, quarantine, rollover, and cleanup fencing.
Provider-neutral tests run unchanged against a Claude-feasibility fake.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/providers/test_codex.py tests/runtime/providers/test_provider_contract.py -q
```

- [ ] **Step 3: Adapt existing hardened components**

Move Codex-specific construction behind the adapter; retain Task 4 verified spawn,
manifest, and sandbox checks. Do not duplicate them in coordinator. Result parsing
produces bounded immutable BlobRefs; managed output is consumed only from the
Task 2 sealed project snapshot. This vertical declares no exported artifacts:
ArtifactRegistry and publication arrive once in Task 10, never through a temporary
provider persistence path.

- [ ] **Step 4: Run GREEN and a disposable real smoke**

```bash
cd engine
uv run pytest tests/runtime/providers tests/runtime/test_effect_gates.py tests/runtime/test_manifests.py -q
uv run pytest tests/integration/test_managed_effect.py -q
```

The integration test uses a disposable Git project and the available Codex CLI;
absence is an explicit environment skip, not a fake pass. Commit
`feat(runtime): run managed Codex effects safely`; review/fix to zero.

---

### Task 7: Manual and pinned effects; public scenario controls

**Files:**

- Create: `engine/src/lockstep/runtime/providers/manual.py`
- Create: `engine/src/lockstep/runtime/providers/pinned.py`
- Modify: `engine/src/lockstep/runtime/service.py`
- Modify: `engine/src/lockstep/runtime/sessions.py`
- Create: `engine/tests/runtime/providers/test_manual.py`
- Create: `engine/tests/runtime/providers/test_pinned.py`
- Create: `engine/tests/runtime/test_service_controls.py`

**Interfaces:**

- `LockstepService.scenario_done`, `scenario_escalate`, `scenario_abort`,
  `scenario_status`, and `scenario_wait`.
- Worker controls resume the same interrupt with closed result/control payloads;
  no status mutation method exists.

- [ ] **Step 1: Write owner/session/control RED tests**

Cover returned worker binding required, mismatch/expiry denied and doctor-visible,
starting/engine running unbound allowed, underscore evidence rejection, declared
writes/manifests on pass/fail/error, scenario_escalate -> terminal FAIL,
scenario_abort -> terminal ABORTED, `_engine` submission denial, pinned argv/cwd/
phase safe status, raw credential/output redaction, wait no-side-effects, and
unmanaged-manual timeout/scope rejection.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/providers/test_manual.py tests/runtime/providers/test_pinned.py tests/runtime/test_service_controls.py -q
```

- [ ] **Step 3: Implement adapters and service facade**

Extend the Task 3 service facade; do not create another application service.
Manual adapter performs no spawn/termination and claims no containment. Pinned
adapter uses Codex sandbox `no_publish_operation`; file/JUnit results require
result-stability capability. Service delegates all progress to GraphRuntime and
coordinator and never writes workflow status.

- [ ] **Step 4: Verify legacy UX regressions and commit**

```bash
cd engine
uv run pytest tests/runtime/providers tests/runtime/test_service_controls.py tests/test_session_binding.py tests/test_engine.py tests/test_validators.py -q
```

Commit `feat(runtime): bridge manual and pinned native effects`; review/fix to
zero.

---

### Task 8: Deterministic DSL lowering for sequence and control flow

**Files:**

- Create: `engine/src/lockstep/workflow/compiler.py`
- Create: `engine/src/lockstep/workflow/lowering.py`
- Create: `engine/src/lockstep/workflow/canonical.py`
- Create: `engine/src/lockstep/workflow/freshness.py`
- Create: `engine/src/lockstep/workflow/estimate.py`
- Modify: `engine/src/lockstep/workflow/__init__.py`
- Modify: `engine/src/lockstep/recipe/profile.py`
- Create: `engine/tests/workflow/test_compiler.py`
- Create: `engine/tests/workflow/test_lowering.py`
- Create: `engine/tests/workflow/test_freshness.py`
- Create: `engine/tests/workflow/test_estimate.py`
- Modify: `engine/tests/test_profile_check.py`
- Create: `engine/tests/workflow/golden/`

**Interfaces:**

- `compile_workflow(workflow, catalog) -> CompilationResult`
- Result contains canonical recipe bytes, source-map bytes, dependency manifest,
  and digest.
- `estimate_workflow(...) -> StructuralEstimate` and
  `estimate_manual_recipe(...) -> StructuralEstimate` return user-work steps,
  maximum validator submissions, pinned commands, child calls, maximum child
  calls through bounded loops, peak parallel branches/subcalls, maximum configured
  runner timeout, generated-node count, and expanded-fragment count. They report
  the engine/runner-controlled time formula with retry/repeat, child/parallel
  timeout, pinned-command, and cleanup assumptions; any missing required timeout
  makes that bound explicitly unavailable. End-to-end human/agent time is always
  labelled unbounded. Token/money estimates are unavailable unless optional
  owner-controlled runner metadata supplies labelled price/context inputs and all
  assumptions.

- [ ] **Step 1: Write golden RED tests**

Cover sequence, managed/manual step, verify, decide, accept, choose, retry, bounded
repeat, on_failure/on_error, escalation terminal, stable source-pointer IDs,
closed descriptor digests, terminal outcomes, and byte-identical compilation.
Every generated effect is a native yamlgraph interrupt. Add a focused loop-exit
fixture proving `loop_exits` targets a passthrough gate then an ordinary edge to
the interrupt prepare path. Add estimate goldens for DSL and manual YAML covering
every normative metric, assumptions, and honest unavailability. Port every legacy
`_subcall` profile/channel assertion to closed native effect descriptors,
compiler-only scope/gate rejection, or the direct-child tests owned by Task 9.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/workflow/test_compiler.py tests/workflow/test_lowering.py tests/workflow/test_freshness.py tests/workflow/test_estimate.py tests/test_profile_check.py -q
```

- [ ] **Step 3: Implement pure compiler modules**

Lower immutable IR to plain dict/YAML only; never import yamlgraph. Canonicalizer
fixes keys/scalars/nodes/edges/source-map order. Profile has two explicit paths:
compiler-only `scope`, generated gates, and source-map markers require
compiler-output/canonical-match provenance; ordinary closed `lockstep_effect`
descriptors in complete manual YAML are admitted after the same strict schema
validation without generated metadata. Manual scope/compiler markers are rejected.
Both paths reject direct loop exits to typed interrupts. Add manual protected
effect start/resume/restart and negative manual compiler-marker tests.
Delete the legacy `lockstep.subcalls` profile alias and `_subcall_*` reserved-state
contract in this task; no compatibility marker is accepted.

- [ ] **Step 4: Compile generated YAML through the adapter and verify GREEN**

```bash
cd engine
uv run pytest tests/workflow tests/test_profile_check.py -q
uv run pytest tests/recipe/test_adapter_boundary.py tests/recipe/test_loader.py -q
```

Commit `feat(workflow): lower DSL control flow to yamlgraph`; review/fix to zero.

---

### Task 9: Native direct child calls and graph/include_graph escape hatches

**Files:**

- Modify: `engine/src/lockstep/workflow/lowering.py`
- Modify: `engine/src/lockstep/workflow/freshness.py`
- Modify: `engine/src/lockstep/runtime/recipe_bundles.py`
- Modify: `engine/src/lockstep/recipe/profile.py`
- Modify: `engine/src/lockstep/runtime/engine.py`
- Modify: `engine/src/lockstep/runtime/service.py`
- Delete: `engine/src/lockstep/runtime/subcalls.py`
- Delete: `engine/src/lockstep/runtime/_subcall_wrapper.py`
- Delete: `engine/tests/test_subcalls.py`
- Create: `engine/tests/workflow/test_child_lowering.py`
- Create: `engine/tests/workflow/test_graph_fragments.py`
- Create: `engine/tests/integration/test_native_child_restart.py`
- Create: `engine/tests/test_daily_change_recipe.py` with native direct-child expectations

**Interfaces:**

- `call` emits `mode: direct` plus deterministic contract pre/post nodes.
- `graph`/`include_graph` emit namespaced native YAML fragments.

- [ ] **Step 1: Write RED tests for child/fragment guarantees**

Cover child input/export contract mapping, type mismatch, missing exports,
transitive freshness, parent stale after child change, manual-parent snapshot,
namespace collisions, fragment entry/exits, read-only rules, process restart at a
child interrupt, opaque non-addressable `child_run_id`, deterministic specialization
for `call.runner`, shared ScopeResult state channels, nested deadline minimum,
sequential effect inheritance, and context restoration. Assert standalone child
worker steps remain manual, while `call.runner=codex` lowers them to managed
effects whose requests bind the sealed runner digest; mismatch rejects before
spawn. Artifact export is deferred to Task 10.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/workflow/test_child_lowering.py tests/workflow/test_graph_fragments.py tests/integration/test_native_child_restart.py tests/test_daily_change_recipe.py -q
```

- [ ] **Step 3: Implement native composition and remove child scheduler**

Bundle the full reachable DAG. Direct children declare no saver. Pre/post nodes
copy only validated shared contract fields. Emit a protected no-spawn scope
interrupt, explicitly declare its resume key in parent/specialized child schemas,
and bind worker descriptors to validated runner/scope/effective deadline. Nested
specialization carries ancestor scope keys and never changes child topology.
Remove subprocess child-run claims, child RunIndex rows, custom child credentials,
and parent auto-resume code.
Remove every Engine/subcall import here.
The state-free Engine facade and LockstepService delegate child execution to
GraphRuntime, so deleting subcall modules cannot break normal imports.

- [ ] **Step 4: Verify and commit**

```bash
cd engine
uv run pytest tests/workflow tests/integration/test_native_child_restart.py tests/test_daily_change_recipe.py tests/recipe/test_native_capabilities.py tests/test_cli.py tests/test_server.py -q
uv run pytest tests/runtime/test_no_legacy_workflow_state.py -q
```

No scheduler residue is allowed. Commit
`refactor(runtime): use native yamlgraph child composition`; review/fix to zero.

---

### Task 10: Immutable artifact provenance and recoverable publication

**Files:**

- Create: `engine/src/lockstep/runtime/artifacts.py`
- Create: `engine/src/lockstep/runtime/publication.py`
- Modify: `engine/src/lockstep/runtime/effects/coordinator.py`
- Modify: `engine/src/lockstep/workflow/lowering.py`
- Modify: `engine/src/lockstep/runtime/validators.py`
- Create: `engine/tests/runtime/test_artifacts.py`
- Create: `engine/tests/runtime/test_publication.py`
- Create: `engine/tests/integration/test_artifact_visibility.py`
- Modify: `engine/tests/test_validators.py`

**Interfaces:**

- Blob bytes are SHA-256 addressed.
- Artifact key is effect/native coordinate + declared name.
- Publication has `prepare`, `apply_or_recover`, `rollback_or_recover`; it cannot
  resume the graph itself.

- [ ] **Step 1: Write RED provenance/publication tests**

Reuse Task 2 BlobStore/ProjectSnapshotStore. Cover coordinate/name uniqueness,
artifact bytes before resume invisible to graph, restart reuse, publication crash
before/after each file replacement, corrupt journal fail-closed, symlink/TOCTOU/
Git-control rejection, graph parked until publish result delivery, and native
child contract export of artifact refs after restart.

Replace `_subcall_envelope.artifact_hashes` validation with declared immutable
ArtifactRef plus producer-coordinate/digest validation; retain the paired content
check and provenance-vs-content distinction.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/test_artifacts.py tests/runtime/test_publication.py tests/integration/test_artifact_visibility.py tests/test_validators.py -q
```

- [ ] **Step 3: Implement ports by reusing Task 4 primitives**

Keep bytes/provenance/publication facts separate. Use manifests and atomic owner
journals already proven in Task 4. Coordinator puts refs into EffectResult only;
registry/publication never decides routing or join completion.

- [ ] **Step 4: Verify and commit**

```bash
cd engine
uv run pytest tests/runtime/test_blobs.py tests/runtime/test_project_snapshots.py tests/runtime/test_artifacts.py tests/runtime/test_publication.py tests/runtime/test_manifests.py tests/integration/test_artifact_visibility.py tests/test_validators.py -q
```

Commit `feat(runtime): add immutable artifact publication`; review/fix to zero.

---

### Task 11: Native static parallel and concurrent effect delivery

**Files:**

- Modify: `engine/src/lockstep/workflow/lowering.py`
- Modify: `engine/src/lockstep/runtime/effects/coordinator.py`
- Modify: `engine/src/lockstep/runtime/status.py`
- Create: `engine/tests/workflow/test_parallel_lowering.py`
- Create: `engine/tests/runtime/effects/test_parallel_delivery.py`
- Create: `engine/tests/integration/test_native_parallel.py`

**Interfaces:**

- Parallel lowering uses yamlgraph fan-out/reconvergence only.
- Coordinator enumerates native pending tasks and calls GraphRuntime batch resume;
  no branch persistence API is introduced.

- [ ] **Step 1: Write RED native-parallel tests**

Cover deterministic branch ordering, distinct/reducer keys, semantic overlap
rejection, genuinely simultaneous interrupts, independent effect leases,
individual resume, batch resume, already-satisfied flat interrupt nuance, restart,
native join exactly once, artifacts/provenance, timeout/error routing, and status
annotations. Bounded parallel first resumes one no-spawn scope interrupt; every
branch request binds the same absolute deadline, overdue reconciliation safely
seals ERROR only after terminal-safety proof, proof-pending status remains running,
and restart scans overdue rows. Assert SQL table names contain no
branch/join/scheduler/timer table.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/workflow/test_parallel_lowering.py tests/runtime/effects/test_parallel_delivery.py tests/integration/test_native_parallel.py -q
```

- [ ] **Step 3: Implement only graph lowering and pending-task processing**

Use yamlgraph list fan-out and multi-source reconvergence. Preserve declaration
order in generated bytes/artifacts. Use reducers only when IR contract declares
them. Coordinator concurrency is external-effect concurrency; LangGraph remains
the barrier.

- [ ] **Step 4: Verify and commit**

```bash
cd engine
uv run pytest tests/workflow/test_parallel_lowering.py tests/runtime/effects/test_parallel_delivery.py tests/integration/test_native_parallel.py tests/recipe/test_native_capabilities.py -q
```

Commit `feat(workflow): use native parallel fanout and join`; review/fix to zero.

---

### Task 12: CLI/MCP, status/wait/events, templates, and authoring workflow

> **Expanded execution plan (incorporated 2026-08-26):** this section is the
> global Task 12 authority. It incorporates the corrected Task 12 freeze, the
> R1b-P boundary simplification, and repository-wide architecture remediation.
> The task-scoped documents remain detailed test-oracle annexes; they do not
> replace this master plan.
>
> Detailed annexes:
> `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12-corrected-replan.md`,
> `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12-r1bp-simplification-plan.md`,
> and
> `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12-god-method-remediation-plan.md`.
>
> **Normative review boundary:**
> `.superpowers/specs/2026-08-20-lockstep-threat-model.md`. A security
> finding is actionable only when it identifies a reachable untrusted path,
> pre-existing authority, non-zero authority delta, affected asset, and
> deployment profile. Section 11's stop rule forbids recursive hardening once a
> bypass requires arbitrary code execution, reflection, or a debugger inside
> the TCB and no trusted internal object is exposed to an untrusted party.

#### Task 12 outcome and release boundary

Task 12 delivers four separately reviewable product units:

1. **12R1 — passive projection and closed public composition.** Observation is
   structurally incapable of command-side recovery, pumping, durable mutation,
   or provider execution. Runtime composition is closed over exactly `codex`
   and `pinned`, with deterministic whole-DAG preflight before the first durable
   start-side write.
2. **12R2 — durable run driving and restart-complete migration.** A discovery-
   only `RunDriveWatch` keeps admitted non-terminal runs recoverable without
   becoming a second workflow scheduler. Migration is bounded, monotonic,
   epoch-fenced, and complete across restarts.
3. **12A — whole-DAG authoring generation.** Compile, check, diff, template
   installation, and publication share one immutable transitive closure. Cooperating
   writers serialize, while publication is deliberately per-file: each replacement
   is atomic and durable, not a multi-file all-old-or-all-new transaction.
4. **12C — installed-contract cutover.** CLI, MCP, packaged templates, plugin
   metadata, skills, README, active docs, examples, and the built wheel expose
   only callable current contracts.

R1 and R2 form one unreleasable runtime range. A may progress only after its own
reviewed RED freeze. The blocking Task 12A.5 proportionality review follows A;
C cannot start until that review produces a user-approved keep, simplify,
redesign, or stop decision. C is last. No Task 13 work, release-ready claim,
versioning, publication, push, or GitHub issue is allowed until every Task 12
gate is green and independently reviewed. Publication additionally requires
explicit user approval of the exact material immediately before publication.

#### Task 12 frozen ownership invariants

- LangGraph/yamlgraph solely owns topology, coordinates, pending kinds, routes,
  joins, outcomes, and terminal state. Lockstep adds no scheduler, coordinator
  transcript, workflow-state store, or recovery cursor.
- `RuntimeProjection` owns status, wait, history, events, list, and trace over
  verified bounded read resources. It cannot construct or reach the command
  service, recovery driver, pump, writable store, or provider.
- `LockstepCommandService` owns start, worker resume, done, abort/escalate,
  acceptance, explicit recovery, active-run queues, and pumping. Public
  observation methods and the legacy `LockstepService` alias are removed.
- `RunDriveWatch` owns discovery only. It contains no coordinate, phase, route,
  outcome, grant, or public status and authorizes nothing.
- `EffectLedger` owns external-attempt facts only. Decision stays trusted,
  runner-free, rowless, and exactly-once under its commitment guard.
- Owner configuration is data, never authority. Provisioning, static admission,
  currentness, dynamic coordinate-bound grants, and bearer consent are separate.
- Public selectors are exactly `codex` and `pinned`; there is no generic registry,
  implicit selector, or unavailable-authority fallback.
- Authoring source/read sets are immutable preconditions. Only the trusted,
  project-bound publisher mutates destinations after complete identity and
  ancestor revalidation. Each replacement is atomic and durable; no journal,
  rollback, or automatic authoring recovery exists. A crash may leave old, new,
  or mixed generated files, and runtime start admits only a freshly observed
  complete canonical closure and exact DAG.
- Refactoring changes structure only: generated bytes, digests, node IDs,
  durable order, transaction/CAS/lease/fsync boundaries, public DTOs, CLI/MCP
  shapes, and future RED oracles remain exact.

#### Task 12 current recovery checkpoint

- [x] Resolve the two known structural REDs: `_resume_worker` must not eagerly
  require `leases`/`coordinator` on a minimal resume path, and
  `recipe.authority` must become an explicit facade over focused modules without
  duplicate type identities.
- [x] Complete or revert every partial extraction; no facade may hide a monolith
  or move all mutable owner state into an unnamed helper.
- [x] Run focused gates, then the complete engine suite, and record exact counts
  against the last reliable baseline: 1159 passed, 1 skipped, 2 warnings.
- [x] Run change detection, impact/affected-flow, and tests-for graph analysis.
- [x] Obtain independent architecture and behavior reviews governed by the
  threat model and authority-delta test.

Checkpoint evidence on 2026-08-26: focused authority/resume regression
`127 passed`; corrected R1b-P independent-review gate `149 passed`; focused
owner/CLI/authority regression `147 passed`; architecture gate `20 passed`
with one review-only cohesion warning; complete current-unit regression
`1171 passed, 1 skipped` after excluding only the two frozen R2 files. Both
independent reviewers reproduced exactly 29 intentionally frozen R2 RED at
their documented future DTO/DDL/API/behavior oracles and returned PASS with no
Critical, Important, Minor, or security findings. No current-unit failure
remains. The owner-policy cold-import cycle and CLI `AuthoringError` identity
regression found during the first review were fixed by test-first changes and
passed re-review.

#### Task 12R1: Passive observation and closed composition

##### 12R1a — inert projection and exact owner ingress

- [ ] Make `Engine.observe(...) -> RuntimeProjection` and
  `Engine.command(...) -> LockstepCommandService` the only construction paths;
  bare `Engine(...)` has no active compatibility default.
- [ ] Put every session/catalog/effect/bundle/checkpoint passive read behind
  `RuntimeReadResources`, with bounded no-follow owner verification and no
  application DDL/DML or state initialization.
- [ ] Remove public observation paths from the command capability and route
  CLI/MCP observation only through projection.
- [ ] Replace validator-by-validator owner CLI handling with one exact parsed
  boundary: bounded regular-file bytes, strict UTF-8/JSON, closed nested schemas,
  normalized key order, duplicate/cardinality checks, and canonical multi-recipe
  inventory union.
- [ ] Keep CLI ownership limited to grammar, bounded path-to-bytes adaptation,
  domain invocation, and output serialization.
- [ ] Preserve digest vectors and real DAG/pinned inventory tests; consolidate
  validation into a table-driven domain matrix plus a small file-boundary matrix.

##### 12R1b-P — provisioning and immutable owner snapshots

- [x] Extract provisioning ingress, snapshot repository, and orchestration into
  focused owners without changing behavior.
- [x] Enforce one bounded regular-file boundary for all owner-controlled input,
  then close trust-root, predecessor, generation, and snapshot consistency.
- [x] Keep credentialed Codex and credential-free pinned bindings distinct and
  preserve exact pinned identity across capture, replacement, and restart.
- [x] Serialize provisioning atomically under one owner lock with fsync and
  complete-snapshot replacement; omission revokes and idempotence preserves the
  generation.

##### 12R1b-A/E — static admission, adapters, and commitment

- [x] Freeze and independently review the A0 tests-only range: exactly two
  positive real `codex`/`pinned` REDs at the inert policy branch, with A8/A9–A12
  GREEN controls. Review rounds closed all Critical/Important findings; final
  RED-freeze commit `d11eee5` is PASS.
- [x] Complete R1b-A0 first: real fully granted `codex` and `pinned` closures
  pass exact static admission and park before launch; A8/A9-A12 remain GREEN
  safety controls rather than forced REDs.
- [x] Freeze and independently review the A1 currentness RED: a real completed
  preflight followed by supported provisioning revocation fails only at the
  stale-decision/zero-start-write oracle (`6cfef01` + `6a61857`).
- [x] Complete R1b-A1 only after A0: supported provisioning drift after a
  proven real preflight is rejected under the shared owner lock before every
  start-side durable write.

A1 completion evidence on 2026-08-26: production commits `3d410b8`, `c511313`,
and `92b946f`, with reviewed RED/fix support through `f320e12`. The final
independent review returned PASS with no Critical, Important, or Minor findings.
Gate A passed `219` tests; the complete current suite excluding only frozen R2
passed `1190`, skipped `1`, and retained one pre-existing review-only warning;
the frozen R2 set remained exactly `29` intentional failures.
- [x] Preflight the complete transitive runtime requirement inventory before any
  durable start-side write.
- [x] Separate stable selection keys from current binding/config digests; callers
  never predict generation-bound digests.
- [ ] Construct only closed `codex`/`pinned` adapters after successful preflight,
  and recheck exact authority at spawn, observation, delivery, and continuation.
- [x] Execute R1b-E as ordered E0–E3 microcycles: A5 public composition/
  commitment GREEN first; then A6 GREEN control plus sole A7 selector-lifecycle
  RED→GREEN; then A15 restart
  reconstruction; then A16 after-resolve drift. No later RED may be frozen while
  it still fails at an earlier stage's common gate.
- [x] Before E0 production, independently review the tests-only Gate A
  transition: replace the two positive A0 park outcomes with static planning
  proofs, retire the two park-only restart/cache cases, preserve all negative A0
  and A1 invariants, and add A5. The evolved gate is `220` collected: `219`
  GREEN plus the sole A5 RED before production, then `220 passed` after E0.
- [x] Carry packaged `reviewed-change` and `parallel-review` through R1b-E as
  scope-only GREEN no-requirement/no-grant/no-spawn controls; prove the public
  binding-digest/request chain with a separate real protected managed closure.
  Any authority-bearing template change belongs to a separately reviewed
  Task 12C product-contract RED.

E0 completion evidence on 2026-08-26: tests/design corrections through
`931bff4` and production commit `23f82a7` established exact immutable-bundle
requirement reconstruction, owner grant/request/prepared-launch commitment
under the shared owner snapshot lock, coherent first protected composition
installation, and the closed `codex`/`pinned` pair. Independent final review
returned PASS with no Critical, Important, or Minor findings. The canonical
evolved Gate A is now `243 passed`; related coordinator/crash/parallel/provider
controls passed `66`; the complete non-R2 suite passed `1191` with `1` skipped;
the frozen R2 set remained exactly the same `29` intentional failures. E1
A6 GREEN control plus sole A7 selector-lifecycle RED→GREEN is the next and only
active R1b-E unit.

E1 preflight correction on 2026-08-26: independent reproduction returned
`PLAN_DEFECT` only for the old demand that both siblings be RED. A6 is already
fully GREEN on `23f82a7` through the real Codex process, durable delivery, and
native completion. The sole honest RED is the public compiler contract
`kind=verify → selector=pinned`: exact admission and durable preparation succeed,
then the pinned strategy rejects the semantic kind before spawn. E1 therefore
carries A6 as a GREEN control and fixes A7 without effect-kind masquerading or
generic-kind acceptance; A15/A16 remain later isolated stages.

E1 completion evidence on 2026-08-26: tests/design freeze `2bfb994` and
production GREEN `d6bd2cb` preserve A6 as a real Codex lifecycle control and
make the public compiler contract `kind=verify → selector=pinned` GREEN through
the immutable closed accepted-kind sets `{managed}` for Codex and
`{pinned, verify}` for pinned. No kind rewriting occurs; A7 proves exact owner
profile argv, full durable environment, credential-free pinned home, PASS with
no result/snapshot publication, ledger `effect_kind=verify`/`delivered`, and
native completion. Independent review PASS with no Critical/Important/Minor.
Canonical Gate A passed `247`; full non-R2 passed `1195` with `1` skipped and
the unchanged architecture warning; frozen R2 remained exactly `29` intentional
failures. E2 A15 restart reconstruction is the next R1b-E unit.

E2 preflight correction on 2026-08-26: independent reproduction returned
`PLAN_DEFECT` for the old prohibition on a runner-lookup RED. A real protected
run reaches durable `running` and one production spawn; catalog binding and its
immutable bundle are valid, and live recipe source is removed. A fresh command
service then fails closed in initial `_recover_effect_batch →
_runner_for_binding` because no reconstructed composition contains the ledger's
exact durable binding digest. This is the first honest observable symptom of
missing restart composition reconstruction, not a fixture or selector failure.
A15 now freezes that sole RED and must GREEN it through immutable-bundle/current-
owner reconstruction, exact binding adoption, and zero additional spawn; A16
and R2 recovery policy remain excluded.

E2 completion evidence on 2026-08-27: production commit `ed8ed05` reconstructs and
validates every selected bounded page from the catalog-bound immutable bundle
plus current owner snapshot/grants/bindings, then installs at most one equal
production composition before reconciliation. Exact cursor/limit propagation,
`128 + 1` overflow rejection, project-bound capture, durable binding drift,
equal/no-reinstall, differing/fail-closed, automatic/explicit recovery, and
global `activation → admission` lock order have direct controls. A15 adopts the
existing process with zero fresh spawns after live recipe deletion and reaches
PASS/result/rollover, durable delivery, and native completion. Independent
final review PASS with no Critical/Important/Minor. Canonical Gate A passed
`256`; full non-R2 passed `1205` with `1` skipped and the unchanged warning;
frozen R2 remained exactly `29` intentional failures. E3/A16 is next.

E3 completion evidence on 2026-08-27: tests/design commit `9b1e3d4` and independent reachability/review
confirmed `PLAN_DEFECT` in the old RED staging because E0's original shared-lock
commitment guard already makes the exact A16 future oracle GREEN. The
tests/design-only carried control pauses after the final real resolve and before
the unchanged original commitment, then uses supported CLI reprovision to change
the Codex binding/configuration and reissue the same selected grant. Commitment
rejects the changed owner snapshot before `ensure_started`; the exact durable
`launching` audit and passive native snapshot/pending coordinate remain
unchanged, with zero spawn, markers, result, delivery, or continuation.
Independent final review PASS with no Critical/Important/Minor. Gate A passed
`258`; full non-R2 passed `1206` with `1` skipped and the unchanged warning;
frozen R2 remained exactly `29` intentional failures. R1b-E0–E3 is complete;
remaining Task 12 work and combined R1 review are next.
- [x] Preserve later-unit behavior REDs until R1 passes, then independently
  review the entire R1 range.

R1 completion evidence on 2026-08-27: the first combined review of
`db1204f..9b1e3d4` returned security and behavior PASS, but architecture found
one Important hidden service-locator boundary in the extracted workspace
provider plus Minor ownership/range hygiene residue. Reviewed tests-only RED
`2785cd6` and production GREEN `91ffe90` replace all `Any owner`, dynamic
`__getattr__`, and facade back-references with one frozen data-only
`WorkspaceContext` and explicit typed repository/attestor dependencies while
preserving exact record bytes and lock/rename/fsync traces. Commits `38fe8dc`
and `31cca13` remove the unused provisioning/ingress re-export surfaces and
close all range whitespace findings. Final independent architecture,
security/threat-model, and behavior reviews returned PASS with no remaining
Critical/Important/Minor findings. On final HEAD `31cca13`, Gate A passed
`258`; full non-R2 passed `1207` with `1` skipped and the unchanged reviewed
cohesion warning; frozen R2 remained exactly the same `29` intentional REDs;
compileall, graph rebuild, status, working diff, and `db1204f` range diff were
clean. Task 12R1 is complete. The B0.5 feasibility probe exposed the B0.4
native child→parent lineage prerequisite; B0.4 and the real B0.5 acceptance-
lifetime RED freeze, R2a, the final R2 policy cutover, and the combined R1+R2
review are complete. Task 12A is the next Task 12 milestone.

#### Task 12R2: Durable run driving and v2 migration

- [x] Complete the reviewed B0.4 prerequisite: prove real child→later-parent
  native lineage through bounded public parent-config/completed-subgraph
  traversal, retaining all stale/fork/sibling/foreign/ambiguous/missing-anchor
  fail-closed controls without a new lineage transcript or native-table access.
- [x] Freeze the real acceptance-lifetime RED using native child artifact,
  producer, consent, and pending-acceptance facts; synthetic snapshots do not
  satisfy the gate.
- [x] Introduce only policy-free watch/migration DTO, repository, and classified-
  page boundaries before driving behavior.
- [x] Use immutable DB admission sequence plus sweep high-water; capacity-
  deferred work stays eligible and cannot starve later work.
- [x] Implement nullable start-input watch v2 and bounded restart-complete
  migration. An epoch/schema fence rejects legacy writers; migration data never
  becomes workflow/scheduler state.
- [x] Keep runs discoverable until native terminal state and durable cleanup.
  Only `RecoveryDriver` automatically resumes.
- [x] Test crash/retry, partial/batch resume, fairness, cleanup, parallel delivery,
  acceptance lifetime, and fresh-driver restart; review R1+R2 together.

Combined R1+R2 review completed 2026-08-27. A composed static-start/pump
deadlock was independently reproduced and closed by the canonical
`admission → current owner snapshot → nonblocking activation` order; contention
releases admission and snapshot before waiting and rechecking currentness.
Final full-project evidence is `1360 passed, 1 skipped` with the single existing
architecture cohesion warning. Contract/threat, feasibility/concurrency, and
architecture/SRP verdicts are all PASS with C/I/M zero.

#### Task 12A: Whole-DAG authoring generation

- [x] Freeze and independently review the public/immutable-surface Gate C RED.
- [x] Add and review the minimal policy-free bundle/publisher surface GREEN.
- [x] Freeze the complete filesystem/concurrency RED matrix on real seams; do
  not freeze guessed private hooks.
- [x] Freeze REDs for recursive planning, collisions, freshness, deterministic
  child-before-parent compilation, and per-file atomic durable publication.
- [x] Make compile, check, diff, canonical match, template install, CLI, and MCP
  consume one immutable transitive source/output plan.
- [x] Keep one project-bound cooperating-writer lock and immutable before/after
  observations; ordinary workflow sources never enter the write set.
- [x] Revalidate every source identity and destination leaf/absence plus ancestor
  immediately before mutation; final read-set validation is the commit point.
- [x] Preserve foreign edits, fsync every published file/parent durable boundary,
  and test filesystem faults without rollback. Incomplete output is repaired only
  by explicit regeneration; legacy v4 requires a pre-simplification recovery
  build and must not be manually deleted.

Task 12A completed 2026-08-29. Corrective microcycles 8–11 closed the bounded
native-app lifecycle leak, committed-marker crash cuts and evidence retirement,
the remaining public second DAG traversal, and the five SRP-confirmed TCB
hotspots. Final engine evidence is `1568 passed, 1 skipped`; compileall and the
complete `1a75172..932e4b0` range diff check are clean. Independent
threat-model, behavior/reliability, and architecture/SRP reviews all PASS with
Critical/Important/Minor zero. Task 12A.5 is now the blocking milestone; Task
12C has not started.

**Task 8 supersession (2026-08-29):** the preceding Task 12A completion text is
historical evidence for the pre-simplification range. The active authoring
contract is now the deliberately narrower per-file contract above. Tasks 1–7
and final fix round 1 (`be924c3829640b3f057cb6da0d8cc5eaece97974`) reduced
authoring to 8 production modules / 1,779 lines and 15 focused tests / 2,065
lines (Gate P baselines: 4,847 / 8,843). The corrected top-anchored
integration-consumer range records 58 additions / 87 deletions / net -29. The
superseded `bb8dd250a03458e88cbcef4d025abdcc26498d63` snapshot of 8 production
modules / 1,760 lines and 15 focused tests / 1,914 lines is historical only.
The Task 8 evidence in that range records compileall, focused, architecture,
installed-contract, and full-engine results; it does not authorize Task 12C or
any publication.

#### Task 12A.5: Complexity, proportionality, and product-goal review

This is a blocking decision milestone, not optional cleanup and not permission
to refactor production code. Its purpose is to prevent a technically elaborate
implementation from displacing the original product goal. Task 12C and all
later implementation stop until the user explicitly approves its conclusion.

- [x] Record reproducible physical and logical source counts, file counts,
  public interfaces, persistent schemas, locks/journals, and independent state
  machines for Lockstep production and tests. Record exact installed versions
  and equivalent measurements for YAMLGraph and the relevant LangGraph
  distributions; keep production and tests separate.
- [x] Partition Lockstep by product capability and responsibility, including
  workflow compilation, runtime supervision, authority/provisioning, durable
  driving, authoring transaction/recovery, CLI/MCP, and installed contract.
  Attribute every substantial subsystem to one current acceptance requirement
  and one reachable threat-model path. Unattributed code is presumptively
  removable.
- [x] Compare Lockstep with the capabilities already supplied by YAMLGraph,
  LangGraph, SQLite, and the operating system. Identify duplicated lifecycle,
  persistence, recovery, scheduling, locking, and filesystem mechanisms and
  state why delegation is insufficient wherever duplication is retained.
- [x] Evaluate at least three complete alternatives against the same product
  acceptance tests: retain the current guarantees, simplify the guarantees and
  threat boundary, or replace custom infrastructure with an existing primitive.
  For each, quantify code removed/retained, schemas and state machines removed,
  security/reliability guarantees lost, migration cost, and maintenance cost.
- [x] Produce a requirement-to-cost matrix and mark every unit `retain`,
  `simplify`, `replace`, `defer`, or `remove`. A PASS based only on green tests,
  method-level cohesion, or absence of individual code smells is invalid.
- [x] Define an explicit complexity budget for Task 12C and any user-approved
  post-Task-12 work: maximum new production surface, new durable schemas/state
  machines, new lifecycle owners, and allowed net code growth. This budget does
  not resurrect the rejected Tasks 14–17. Exceeding it requires a fresh
  user-approved design decision, not a local exception.
- [x] Obtain independent product-scope/proportionality, architecture/SRP, and
  threat-model reviews. Then obtain explicit user approval of exactly one
  outcome: keep the current architecture, execute a dedicated simplification
  range before 12C, redesign/re-scope the remaining work, or stop the project.

Stop the milestone and reject a `keep` verdict if measurements are incomparable,
tests and production are combined, a large subsystem lacks requirement/threat
attribution, duplicated infrastructure is justified only by hypothetical
attacks outside the frozen threat model, or the recommendation relies on sunk
cost. If simplification or redesign is selected, write and approve its own
bounded plan and complete it before Task 12C; do not patch opportunistically
during this audit.

Gate P evidence on 2026-08-29 is recorded in
`.superpowers/reviews/2026-08-29-task-12a5-proportionality.md` at `97893e3`.
All quantitative, attribution, alternatives, matrix, and complexity-budget
work is complete. Independent final product/proportionality, architecture/SRP,
and threat-model reviews each PASS with Critical/Important/Minor zero. At that
commit, the remaining milestone was the explicit selection among `keep`,
`simplify-with-write`, `simplify-owner-applied-patch`, `redesign/re-scope`, or
`stop`; that status is retained as historical decision evidence.

**Gate P closure (2026-08-29):** the user selected `simplify-with-write`, its
separately approved remediation completed through final fix round 1
`be924c3829640b3f057cb6da0d8cc5eaece97974`, and final evidence was refreshed at
`4674e43fa1ffef1b9013f29345b2c7934808131e`. Independent final
product/proportionality, architecture/SRP, and threat/reliability reviews all
PASS with Critical/Important/Minor zero. The user then approved the current
Task 12C continuation order, not the later written Task 12C design. Gate P is
closed; Task 12C implementation and publication were not authorized.

#### Task 12C: CLI/MCP, templates, documentation, and installed contract

> **Proposed current design (2026-08-29; user approval pending):**
> `.superpowers/specs/2026-08-29-task-12c-current-design.md` is the
> independent-review-corrected candidate contract. Review round 2 closes the
> remaining analyzer semantics/evidence gaps and removes credentialed live
> acceptance from Task 12C. Review round 3 additionally freezes distinct
> inner-project/Git-tree review paths and exact function→class→file lambda
> attribution; re-review is pending. The
> user approved only the phase order, not this written spec. If the user
> explicitly approves the spec, it will supersede
> this section's stale create-file inventory, `recipe init --template` grammar,
> atomic full-bundle wording, scope-only packaged-template assumption, and old
> sequencing. Until then the existing text remains historical evidence, not
> implementation authority. The next permitted action is review and explicit
> user approval of the spec; no implementation plan or production/test change
> is authorized.

**Files:**

- Create: `engine/src/lockstep/runtime/events.py`
- Modify: `engine/src/lockstep/runtime/service.py`
- Modify: `engine/src/lockstep/cli.py`
- Modify: `engine/src/lockstep/mcp/server.py`
- Create: `engine/src/lockstep/templates/__init__.py`
- Create: `engine/src/lockstep/templates/reviewed_change/template.yaml`
- Create: `engine/src/lockstep/templates/reviewed_change/parent.workflow.yaml`
- Create: `engine/src/lockstep/templates/reviewed_change/review.workflow.yaml`
- Create: `engine/src/lockstep/templates/parallel_review/template.yaml`
- Create: `engine/src/lockstep/templates/parallel_review/parent.workflow.yaml`
- Create: `engine/src/lockstep/templates/parallel_review/security-review.workflow.yaml`
- Create: `engine/src/lockstep/templates/parallel_review/architecture-review.workflow.yaml`
- Modify: `engine/pyproject.toml` package-data declaration
- Modify: `.codex-plugin/plugin.json`
- Modify: `recipes/` examples to consume the same packaged sources
- Modify: `skills/lockstep-author/SKILL.md`
- Modify: `skills/lockstep/SKILL.md`
- Modify: `README.md`, `docs/`
- Create: `engine/tests/runtime/test_events.py`
- Create: `engine/tests/runtime/test_wait.py`
- Create: `engine/tests/test_recipe_cli.py`
- Create: `engine/tests/test_templates.py`
- Modify: `engine/tests/test_plugin_packaging.py`
- Modify: `engine/tests/test_cli.py`, `engine/tests/test_server.py`

**Interfaces:**

- Recipe commands: init, compile, check, diff, render, estimate.
- Template commands: `template list`, `template show`, and
  `recipe init --template`; each package-resource `template.yaml` is the sole
  manifest for that bundle and maps every bundled file to stable output roles.
  `reviewed-change` roles are parent/review; `parallel-review` roles are
  parent/security-review/architecture-review. Custom template paths reject with
  the v2 diagnostic.
- Scenario commands/tools: start, status, done, escalate, abort, wait, history,
  recover.
- Wait timeout is integer 1–60 and observational only.

- [ ] **Step 1: Write end-user contract RED tests**

Cover all six statuses, exact owner shapes, safe gate fields/redaction,
binding-integrity doctor error, wait change/revision/timeout/no mutation, event
delivery failure non-authority, manual/generated detection, compile collision,
freshness, JSON diagnostics, render/estimate, canonical template source, no legacy
identity, and manual yamlgraph start. Assert estimate JSON/text contains every
structural field and assumption and marks unknowable token/time/money bounds
unavailable for both DSL and manual YAML.

Template tests assert exactly `reviewed-change` and `parallel-review`, role-map
display, package-resource loading outside a source checkout, parent/child
self-containment, all-destination collision preflight, no partial writes, atomic
full-bundle install, and deterministic child-before-parent compilation.

- [ ] **Step 2: Run RED**

```bash
cd engine
uv run pytest tests/runtime/test_events.py tests/runtime/test_wait.py tests/test_recipe_cli.py tests/test_templates.py tests/test_plugin_packaging.py tests/test_cli.py tests/test_server.py -q
```

- [ ] **Step 3: Wire public surfaces and delete duplicated workflow state**

CLI/MCP select the explicit capability before constructing resources:
observation commands call `Engine.observe(...)`, while mutation/recovery commands
call `Engine.command(...)`. Events merge native/effect observations without
routing or mutation. There is no active `LockstepService` compatibility alias or
bare `Engine(...)` default. Implement `recipe estimate` by calling Task 8's pure
estimator and expose the same JSON schema via MCP. Package exactly one
reviewed-change and one parallel-review source; `template show` renders its
manifest role map, while init preflights every destination before an atomic
bundle write and compiles children before parents.
Replace the obsolete “Runner subcalls” Codex-plugin capability with native durable
workflows/effect bridging and update its packaging assertion.

- [ ] **Step 4: Update skills/docs and verify**

The authoring skill covers DSL, compilation, graph/include_graph, and full manual
yamlgraph; it never edits generated YAML. Run:

```bash
cd engine
uv run pytest tests/runtime/test_events.py tests/runtime/test_wait.py tests/test_recipe_cli.py tests/test_templates.py tests/test_cli.py tests/test_server.py tests/test_package_identity.py tests/test_plugin_packaging.py -q
uv run pytest -q
```

Commit `feat(cli): expose native workflow authoring and status`; review/fix to
zero.

#### Task 12 architecture remediation and composite quality gate

Architecture correction is Task 12 acceptance work, not optional cleanup and
not a line-count exercise. Length is a review signal only. A method/class/file is
a hard candidate when composite deterministic evidence shows mixed effect
domains, high cognitive/cyclomatic complexity or nesting, excessive fan-out,
low cohesion/shared mutable state, multiple lifecycle clusters, or a
responsibility hidden one hop behind local delegation.

Execute leaf-first while preserving all behavioral gates:

1. **Workflow lowering:** separate child specialization, call planning, parallel
   planning, graph/fragment validation, condition rewriting, and emission state
   into immutable plans and focused collaborators.
2. **Ledger/provider leaves:** separate prepared facts from persistence;
   decompose Codex launch preparation/supervision/finalization and workspace
   materialization/rollover/attestation/record storage without moving transaction
   or lifecycle authority.
3. **Coordinator lifecycle:** separate context resolution, runner reconciliation,
   publication, manual/acceptance, delivery, and reconcile owners behind a thin
   `EffectCoordinator` facade.
4. **Service/projection:** split start, recovery/drive, worker submission,
   acceptance, and pure status projection; facades expose contracts but contain
   no hidden lifecycle kernel.
5. **Recipe authority:** separate value/policy models, strict YAML admission,
   schema/profile projection, and filesystem ingress; preserve identity through
   explicit re-export tests.
6. **Repository-wide ratchet:** scan every production method, class, and file;
   keep a reviewed semantic exception manifest with responsibility, invariant,
   focused gate, and review expiry. A new or worsened composite outlier fails
   even when its entry method is short.

- [ ] Re-adjudicate all structural candidates, including short high-complexity
  functions, and require zero unreviewed method/class/file candidates.
- [ ] Document each retained cohesive outlier with its single responsibility,
  invariant, focused regression gate, and expiry condition.
- [ ] Run subsystem gates, complete non-future engine suite, compileall, build,
  clean-wheel smoke, and `git diff --check`.
- [ ] Obtain independent PASS reviews for architecture/responsibility and
  behavior/reliability. Security review remains bounded by the threat model.

#### Task 12 exact gate order and stop conditions

1. Recover a fully green, explainable structural baseline.
2. Complete 12R1a; retain later-unit REDs at their reviewed final oracles.
3. Complete 12R1b-P, then 12R1b-A0, 12R1b-A1, and 12R1b-E; review the whole R1
   range. The A0/A1 split is a staging correction only and does not broaden the
   final runtime or authority model.
4. Freeze real acceptance-lifetime RED, complete the policy-free 12R2 skeleton,
   then driving/migration; review R1+R2 together.
5. Freeze and complete 12A in its own range.
6. Complete Task 12A.5's quantitative complexity/proportionality review and
   obtain explicit user approval of the resulting keep/simplify/redesign/stop
   decision. If remediation is selected, complete its separately approved range
   before continuing.
7. Complete architecture remediation and the composite ratchet across all
   production code touched or exposed by Task 12.
8. Only after the 12A.5 decision and any required remediation, freeze and
   complete 12C.
9. Run full source and installed-artifact release gates and independent reviews.

**Approved continuation order; candidate contract pending (2026-08-29):** Gate
P and the selected remediation are complete. The user approved this exact
dependency order:
(1) repository-wide architecture ratchet and remediation, (2) DSL artifact
export plus real authority-bearing built-in templates, (3) complete tests-only
Gate D RED, (4) minimal installed-contract GREEN, and (5) full source, wheel,
and clean-installed reviews. Then stop before publication, push, issue/PR,
merge, tag, or release. The proposed design spec above becomes the exact
contract and conditionally supersedes stale wording only after explicit user
approval of that written file. An implementation plan remains forbidden before
that approval.

Stop at the first missing/wrong-reason RED, unexplained regression, production
edit preceding its RED, projection-to-command dependency, preflight write,
authority implied by configuration, selector outside the closed set, Decision
ledger row, scheduler semantics in watch/migration data, non-atomic authoring
mutation, foreign-edit deletion, unexplained worktree change, or architecture
candidate without adjudication; also stop if Task 12C begins without the
user-approved Task 12A.5 decision, explicit approval of the current written
Task 12C spec, or its complexity budget is exceeded. Return to the owning
unit's design; do not add a local patch merely to advance to the next oracle.

---

### Post-Task-12 milestone: Re-evaluate the downstream roadmap

This milestone is inserted now, but its result is intentionally not prewritten.
Execute it only after Task 12 is fully green, reviewed, and evidenced.

- [ ] Compare completed Task 12 architecture and public contracts with the
  native design, threat model, and every remaining acceptance requirement.
- [ ] Re-run architecture, impact, affected-flow, and coverage analysis against
  the actual completed code rather than pre-Task-12 assumptions.
- [ ] Update global Task 13 and add, remove, split, merge, or reorder later global
  tasks only when concrete uncovered work requires it.
- [ ] Never resurrect Tasks 14–17 from the rejected custom-kernel plan; every new
  task requires a current native-architecture justification.
- [ ] Obtain user agreement on the revised downstream roadmap before executing
  it. This milestone does not authorize publication.

---

### Task 13: Full architecture audit and real Codex acceptance

Task 13 Step 3 is outside Task 12C. It may use actual owner-installed
credentialed Codex and pinned CLIs only after the post-Task-12 roadmap
reevaluation above and separate explicit user approval. Task 12C stops with
deterministic production-adapter source/wheel/staged-plugin evidence using
controlled owner-selected local executables, non-secret binding material, and
no external network. Nothing in Task 12C pre-approves this Task 13 live work.

**Files:**

- Create: `.superpowers/reports/2026-08-20-native-acceptance.md`
- Create: `.superpowers/reports/2026-08-20-native-ux-evaluation.md`
- Modify only production/tests when acceptance exposes a proven defect.

**Interfaces:**

- Produces reproducible evidence for all required real scenarios and a candid UX
  decision; no new architecture is introduced in this task.

- [ ] **Step 1: Run static architecture audits**

```bash
cd engine
uv run pytest tests/runtime/test_no_legacy_workflow_state.py -q
uv run ruff check src tests
```

The architecture test positively enumerates native imports and asserts the set is
exactly `recipe/yamlgraph_adapter.py`; it also asserts rejected architecture terms
do not identify live production types. Empty forbidden-pattern results are success,
not ambiguous shell exit codes.

- [ ] **Step 2: Run full automated verification from a clean process**

```bash
cd engine
uv sync --locked
uv run pytest -q
```

Record exact counts, duration, skips, platform, dependency SHAs, and failures.

- [ ] **Step 3: Run four real scenarios**

Run reviewed-change with real Codex child/review/acceptance/artifact and restart;
parallel-review with overlapping branches, partial/batch resume, native join and
provenance; complete manual yamlgraph with the effect bridge; and DSL mixing
structured blocks with graph/include_graph. Preserve commands, snapshots,
checkpoint-derived status, effect observations, artifacts, and timings.

- [ ] **Step 4: Fix defects through fresh RED/GREEN/review loops**

For each observed defect, add the smallest failing automated test, confirm RED,
implement the architectural fix, run focused plus full regression, and obtain an
independent zero-finding review. Do not weaken assertions or add scenario-only
branches.

- [ ] **Step 5: Write final evidence and UX reports**

Report DSL/manual-YAML intuitiveness, authoring/debugging effort, required
yamlgraph knowledge, branches/loops/children/artifact/parallel expressiveness,
reliability/recovery, runtime overhead, whether complexity was reduced or moved,
defects, and prioritized improvements. Decide from forward-use evidence whether
to update/create the authoring skill; if changed, use `superpowers:writing-skills`
and `skill-creator` with forward tests.

- [ ] **Step 6: Final independent audits and completion verification**

Dispatch one requirements/native correctness reviewer and one patchwork/
maintainability reviewer. Fix and re-review every severity to zero. Then invoke
`superpowers:verification-before-completion`, rerun its required commands, and
commit `test: verify native Lockstep acceptance`. Do not merge to main.

---

### Reserved final hardening: Replace closed string domains with `StrEnum`

Run this only after the functional roadmap is GREEN, before the final reusable
analyzer evaluation. Do not churn evolving domains piecemeal while Task 12 and
later behavior are still being completed.

- [ ] Inventory production string literals that represent genuinely closed
  domains, including reconciliation actions such as `delivered` and `busy`;
  distinguish them from open protocol, user, provider, and persisted values.
- [ ] Freeze exact serialization/backward-compatibility tests before changing
  types; persisted and external representations must remain the same strings.
- [ ] Replace appropriate closed domains with focused `StrEnum` types and use
  enum members in comparisons, transitions, and return contracts.
- [ ] Reject accidental cross-domain comparisons through typing/tests without
  creating one global catch-all enum or coupling unrelated modules.
- [ ] Run the full regression and independent architecture/contract review.

---

### Reserved final roadmap item: Evaluate reusable deterministic complexity analyzer

This item remains last regardless of how the post-Task-12 milestone revises or
extends the downstream roadmap. It is analysis and a decision, not authorization
to extract code or create a skill/package.

- [ ] Separate generic metric mechanics from Lockstep-specific thresholds,
  responsibility taxonomy, semantic exceptions, and CI policy.
- [ ] Require evidence of a stable input/output contract and preferably a second
  concrete consumer; compare keep-local versus extraction maintenance and
  versioning costs.
- [ ] Confirm that an enforceable analyzer is deterministic and runs without an
  LLM; put semantic judgment in an optional review skill only if useful.
- [ ] Record a reasoned keep-local or extract decision.
- [ ] If extraction is justified, obtain user approval for a separate design and
  implementation plan before changing repository boundaries or publishing.
