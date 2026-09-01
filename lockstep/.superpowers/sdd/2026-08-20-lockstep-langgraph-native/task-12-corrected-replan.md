# Task 12 Corrected Implementation Replan and Freeze

Date: 2026-08-25
Frozen baseline: clean `b7948567da426e965ec2ae22e2a0f0407fa0be12`
Repository: `<HOME>/Projects/pets/claude-essentials-worktrees/lockstep-workflow-dsl/lockstep`
Inputs read in full: `task12-architecture-synthesis.md`,
`task12-synthesis-minimality.md`, and `task12-synthesis-review.md`

> **For agentic workers:** implement this plan one unit at a time. Every unit's
> complete RED commit and independent review is a hard prerequisite for that
> unit's production changes. Stop at the first gate violation.

## Verdict

**GO to freeze this corrected replan; NO-GO to patch production from the old
Task 12 plan.** The release is split into four separately reviewable product
units: R1 passive projection plus closed public composition, R2 durable run
driving plus the b794 migration, A whole-DAG authoring publication, and C the
installed-contract cutover. R1 and R2 form one unreleasable runtime range; A is
causally independent after this ownership freeze; C is last.

No production file is changed by this freeze.

> **Supersession for active authoring guidance (2026-08-29):** Sections below
> labelled A/authoring transaction record the frozen pre-simplification plan and
> remain historical evidence. The completed `simplify-with-write` range replaces
> its all-old-or-all-new journal/recovery contract with this active boundary:
> cooperating writers serialize; each file replacement is atomic and durable; a
> crash may leave old, new, or mixed generated files; start admits only a freshly
> observed complete canonical closure and exact DAG; explicit regeneration repairs
> incomplete output; legacy v4 requires a pre-simplification recovery build and
> must not be manually deleted.

## R1 passive-observation threat model

This threat model limits R1 to the reachable passive-observation risk. It is
the decision boundary for implementation and review; stronger guarantees need
a new finding and a separate design.

- **Protected assets:** durable runtime catalog/effect facts, admitted immutable
  recipe bundles, native checkpoints, session bindings, correct current
  observations, and the projection/command capability boundary.
- **Caller and environment:** local CLI/MCP callers choose project, run, and
  timeout values and may repeat observations; project recipe files are
  untrusted after admission; a cooperating command service may write SQLite
  concurrently; existing owner state may be absent, malformed, insecurely
  permissioned, or partially crash-recovered.
- **Required guarantees:** projection performs no application DDL/DML,
  checkpoint, owner-state initialization/sealing, session touch, provider
  action, recovery, pump, or native continuation. Reads use SQLite-managed
  locking/transactions, fail closed for poisoned existing state, remain
  bounded, clean up deterministically, and retain the established public
  observation shape and order.
- **Deliberate exclusions:** malicious root, kernel/filesystem compromise, and
  a same-user process deliberately replacing internally consistent trusted
  state are outside this unit. R1 does not promise one atomic snapshot across
  `runtime.sqlite` and `native.sqlite`; conservative cross-database results are
  sufficient.
- **SQLite boundary:** durable database and committed WAL facts must not be
  changed by an observation. SQLite may create, remove, or change volatile
  sidecar coordination files while opening a reader, even when the database was
  quiescent immediately before the call. Exact sidecar path/byte stability is
  not a requirement because it conflicts with a writer that may create a WAL
  concurrently. Raw copying of a live database/WAL/SHM family and
  sidecar-presence-based `immutable=1` selection are forbidden because neither
  is an atomic SQLite snapshot.

## Reachable findings and scope trace

| ID | Reachable baseline finding | Required correction | Owning unit |
| --- | --- | --- | --- |
| F1 | Constructing `Engine` for CLI/MCP status, wait, history, or events invokes recovery and starts a pump through `LockstepService.__init__` (`runtime/service.py:207-293`). A cold read mutates an unrelated admitted run. | Structural projection/command split; explicit public capability selection; separate MCP lazy handles. | R1 |
| F2 | Public packaged templates select `codex`, but `Engine` supplies neither production runners nor non-publication authority. Start writes durable facts before the missing-selector failure and the next constructor is poisoned. | Closed public composition and deterministic full-DAG preflight before the first durable owner fact. | R1 |
| F3 | `VerifyIR` lowers to selector `pinned` (`workflow/lowering.py:504-523`); `PinnedRunnerAdapter` requires a credential-free Codex home and owner-selected permission profile. | Compose exactly `codex` and `pinned`, each with an independently captured binding. | R1 / blocker B1 |
| F4 | A delivered manual effect followed by rowless Decision has no effect-ledger recovery row; the admission watch has already been acknowledged, so restart strands the run. | Whole-life discovery-only run-drive watch; effect ledger remains external-attempt authority. | R2 |
| F5 | The b794 watch schema requires a start-input blob, and a naive ephemeral backfill cursor repeatedly scans an old terminal prefix after restart. | Nullable v2 start reference plus restart-complete owner-state schema migration metadata. | R2 / blocker B2 |
| F6 | `compile_project_source()` recursively plans children, but `write_compilation()` publishes only one root (`authoring.py:136-168,309-328`); a child edit leaves a mixed DAG. | Immutable full read/write plan and per-file atomic durable publisher; runtime refuses incomplete/mixed output rather than rolling it back. | A |
| F7 | The current template journal is project-writable, deletion-oriented, and cannot restore overwritten output modes or bytes (`templates/__init__.py:161-214`). | Delete the authoring journal/recovery family; retain a project-bound cooperating-writer lock, source stability checks, and shared per-file publisher. | A / blocker B3 |
| F8 | README, both installed skills, examples, packaged legacy runner code, and active `docs/DESIGN.md` expose retired subcall/runner contracts. | Distribution-level installed-contract cutover after runtime and authoring APIs settle. | C / blocker B4 |

Every new type, file, migration, or test below maps to one of F1-F8. A generic
provider registry, extra provider, scheduler/status store, Decision effect row,
durable ongoing recovery cursor, Task 10 publication reuse, historical rewrite,
or stronger same-user isolation claim has no reachable finding and is forbidden.

## Frozen ownership invariants

1. LangGraph/yamlgraph is the sole owner of topology, coordinates, pending
   kinds, routes, joins, outcomes, and terminal state.
2. `RunDriveWatch` owns only durable discovery of an admitted run. It contains
   no native coordinate, phase, route, outcome, grant, or public status and
   authorizes nothing.
3. `EffectLedger` owns external attempt facts only. Decision remains trusted,
   runner-free, exactly-once under the existing commitment guard, and rowless.
4. `RecoveryDriver` is the sole automatic resume owner. Projection construction
   and projection calls cannot instantiate, reach, signal, or depend on it.
5. The command composition root owns the only released selector set:
   `codex` and `pinned`. Recipe bytes select a member of that closed set; owner
   state binds each adapter and permits exact static runtime requirements.
6. Static preflight proves definition-level binding and OS-execution policy
   before any durable start-side write. The existing coordinate/input/deadline
   `EffectGrant` is derived and rechecked dynamically at commitment.
7. Publication bearer consent is not a static admission requirement.
   `OwnerConsentAuthority` remains the sole wrapper that adds acceptance and
   publication authority around the same non-publication delegate.
8. After admission commits, a run remains discoverable until native terminal
   state and durable `reconcile_consumed` cleanup. A constructor exception is
   never a recovery protocol.
9. A parent authoring command denotes one immutable transitive source/output
   closure. Compile, check, diff, canonical match, and template install use the
   same planner and publisher.
10. Ordinary workflow sources are immutable read preconditions, never compile
    destinations. Template sources are destinations only under the installer's
    stricter all-destinations-absent precondition.
11. Authoring publication serializes cooperating writers and makes each file
    replacement atomic and durable. It has no multi-file rollback or automatic
    recovery; a crash may leave old, new, or mixed generated files. Fresh
    canonical-DAG admission and explicit regeneration handle incomplete output;
    legacy v4 requires a pre-simplification recovery build. Task 10's
    consent/artifact publisher is a separate authority.
12. Active installed guidance and the built distribution describe only current,
    callable contracts.

## Frozen file structure and interfaces

### Runtime resources, projection, and command driver

- Create `engine/src/lockstep/runtime/read_resources.py` with private
  `RuntimeReadResources`. It opens only existing, verified owner-state facts
  needed by observations through bounded read-only access. Construction and
  calls may not initialize a store, execute application DDL/DML, or create a
  command-side capability. `LockstepCommandService` constructs its writable
  stores and command-side handoff dependencies independently; there is no
  shared general-purpose resource object spanning the projection and command
  boundaries.
- Create `engine/src/lockstep/runtime/projection.py` with `RuntimeProjection`.
  It owns only status, wait, history, events, list, and trace operations over a
  `RuntimeReadResources` instance. It has no command object reference.
- Refactor `engine/src/lockstep/runtime/service.py` into the active
  `LockstepCommandService`. It owns its writable resources plus the closed
  composition below and owns start, worker resume, done, abort/escalate,
  acceptance, explicit recovery, recovery lock, active-run queues, and pump.
  Remove `_UnavailableEffectAuthority`, constructor recovery, and constructor
  pump startup from the passive path. Pump failures are captured per run and
  never become a projection prerequisite.
- Modify `engine/src/lockstep/runtime/engine.py` so the only constructors are:
  `Engine.observe(state_dir: Path, recipes_dir: Path) -> RuntimeProjection` and
  `Engine.command(state_dir: Path, recipes_dir: Path) -> LockstepCommandService`.
  `Engine(...)` without an explicit capability raises `TypeError`; there is no
  active compatibility default.
- Modify `engine/src/lockstep/cli.py` to choose observation for `status`,
  `wait`, `history`, and `events`, and command for `start`, `done`, `abort`,
  `escalate`, consent operations, and `recover`, before constructing resources.
- Modify `engine/src/lockstep/mcp/server.py` to keep `_projection` and `_command`
  as separate lazy handles/config keys. Read tools, including `list_runs` and
  `run_trace`, call only `_projection_for(project)`; command tools call only
  `_command_for(project)`. Closing/resetting one handle does not instantiate the
  other.

### Closed Codex plus pinned composition and trusted owner snapshot

#### R1b-P owner-snapshot threat model

This threat model is the decision boundary for snapshot provisioning. A
mechanism not traceable to one of these subjects, assets, and guarantees is out
of scope.

- **Protected assets:** the owner-selected Codex and pinned binding facts, the
  complete current grant set, monotonic configuration/policy/grant generations,
  and the trusted separation between owner state and the writable project.
- **Subjects and capabilities:** a local provisioning caller controls CLI file
  paths and document bytes; project-controlled content and symlink components
  are untrusted; two cooperating Lockstep provisioners may run concurrently or
  crash; an existing owner snapshot may be absent, malformed, insecurely
  permissioned, non-regular, truncated, or internally inconsistent.
- **Required guarantees:** every external document is read once through one
  bounded, non-blocking, no-follow regular-file boundary; owner state is proven
  outside the resolved project before any trusted directory is created;
  bindings and replacement keys are closed-schema and freshly captured;
  every predecessor grant is self-consistent before replacement; complete
  replacement and generation reduction are deterministic; one advisory-lock
  critical section covers predecessor read through atomic file and parent
  fsync. Invalid input or state creates no snapshot and never repairs or
  overwrites poisoned predecessor bytes.
- **Deliberate exclusions:** malicious root, kernel/filesystem compromise,
  hostile network filesystems that violate local POSIX rename/locking
  semantics, and a same-user process deliberately constructing a fully
  self-consistent owner snapshot are outside v1. Provisioning does not attempt
  cryptographic same-user isolation, an append-only journal, rollback history,
  a second state writer, or autonomous recovery.
- **Availability boundary:** FIFOs, devices, sockets, directories, symlinks,
  oversized files, and malformed JSON must fail without waiting for a peer.
  Advisory-lock waiting is intentionally unbounded for a cooperating live
  provisioner; kernel lock release after process death supplies crash recovery.

#### R1b-P module boundary

- `owner_policy.py` remains the public facade and owns only frozen values,
  canonical digests, the static requirement index, its private bound view, and
  the inert authority type. It performs no filesystem I/O, locking, provider
  capture, or provisioning orchestration.
- `owner_policy_ingress.py` owns the two exact provisioning JSON schemas and
  returns only normalized mappings and the sorted replacement-key tuple. It
  performs no owner-state write or provider lifecycle action.
- `owner_snapshot_store.py` owns exact snapshot encode/decode, full predecessor
  self-consistency, generation reduction, the advisory-lock transaction,
  bounded snapshot read, atomic replacement, and file/directory fsync. It does
  not import CLI, recipes, command service, projection, or provider adapters.
- `owner_provisioning.py` validates owner-state/project separation, captures the
  two installation bindings, validates the complete replacement against the
  static inventory, and invokes the snapshot store. It does not construct a
  runner/composition or add admission/currentness/commitment behavior.
- `bounded_files.py` provides the one descriptor-based bounded regular-file
  read used by CLI ingress and snapshot storage. Owner UID/mode enforcement is
  an explicit caller option rather than a second reader implementation.
- The existing `owner_policy` public imports remain stable through explicit
  re-exports. The extraction is semantics-preserving before the three failing
  invariants are corrected; no schema, generation, digest, or CLI contract
  changes are permitted during extraction.

#### R1a owner-policy serialization freeze

- Every owner-policy digest hashes the UTF-8 bytes of its exact fixed-key JSON
  object encoded with `sort_keys=True`, compact separators, `ensure_ascii=False`,
  and `allow_nan=False`. Tuple fields are JSON arrays. No newline or prefix is
  added outside the schema member already named by the digest contract.
- `required_capabilities` and `required_authorities` each contain at most 256
  non-empty UTF-8 strings of at most 512 bytes, normalized lexically, with no
  duplicates. This uses the same cardinality/string ceiling as the frozen
  per-key audit-use inventory rather than introducing an unrelated limit.
- `OwnerRuntimeGrant.authority` is the exact literal string
  `os_user_execution`; it is not a boolean or an open authority collection.
- R1a exports the `OwnerRuntimeAuthority` type name but gives it no operational
  `preflight`, `resolve`, currentness, commitment, or grant-minting behavior.
  Those methods are introduced only by their R1b-A/R1b-E behavioral REDs.

- Create `engine/src/lockstep/runtime/providers/composition.py`; it is not a
  registry. Its closed `ReleasedRunnerComposition` has exactly two fields,
  `codex: CodexRunnerAdapter` and `pinned: PinnedRunnerAdapter`, and rejects
  every other selector. Construction remains command-side only: an inert
  projection neither opens a composition nor creates an authority, runner,
  coordinator, recovery pump, or provider preflight object.
- Create `engine/src/lockstep/runtime/effects/owner_policy.py` with:
  `RuntimeRequirement`, `RuntimeRequirementIndex`, `OwnerRuntimeGrant`,
  `OwnerRuntimeSnapshot`, and `OwnerRuntimeAuthority`. A canonical
  `grant_selection_key` hashes exactly `(schema =
  lockstep.runtime-grant-selection/v1, project_identity,
  definition_digest, protected_descriptor_digest, runner_selector,
  required_capabilities, required_authorities)`. Capability/authority tuples
  are bounded, sorted, and unique. The key excludes `runner_binding_digest`,
  `config_generation`, `policy_generation`, `grant_generation`, `logical_file`,
  and `uses`, so an owner can select the same definition-level authority across
  a binding/config change without predicting command-assigned values.
- `RuntimeRequirementIndex.for_authorized_closure(...)` is the pure static
  inventory: each key retains exactly the stable-key input tuple and canonical
  audit uses, but has no binding, config generation, or `requirement_digest`.
  Its private deterministic `index.bind(snapshot)` view validates the captured
  bindings and maps each already-inventoried key to its one current requirement
  digest. The listing/bootstrap/provisioning paths use only the static index;
  preflight, restart reconstruction, resolve, and commitment use the bound view.
  This is an in-memory view, not a second stored table/type or authority ingress.
- A canonical `requirement_digest` then hashes exactly `(schema =
  lockstep.runtime-requirement/v1,
  grant_selection_key, runner_binding_digest, config_generation)`. It is the
  current exact commitment authority identity, not the provisioning selector.
  `logical_file` and `uses` remain bounded audit/provenance fields on the static
  index entry and appear in neither digest. Each `uses` element is exactly the
  JSON object `{"logical_file":"...","logical_id":"..."}` corresponding
  byte-for-byte to the authorized descriptor inventory; no unknown fields. Uses
  are lexically sorted by `(logical_file, logical_id)`, unique, at most 256 per
  key, and each UTF-8 string is at most 512 bytes.
- Store the normalized snapshot at
  `$LOCKSTEP_STATE_DIR/runtime-owner/snapshot.json`. Reuse
  `initialize_owner_state`, `ensure_owner_directory`, `verify_owner_file`,
  `seal_owner_file`, and directory fsync. The root and directory are `0700`;
  the regular non-symlink snapshot is `0600`; owner state must resolve outside
  the writable project. No project-local fallback or environment-carried
  authority is permitted.
- The snapshot schema is exactly `lockstep.runtime-owner/v1` with top-level
  keys `schema`, `config_generation`, `policy_generation`, `codex`, `pinned`,
  and `grants`. Unknown/missing keys fail closed. The `codex` member contains
  the explicit absolute executable, model, CLI version, exact
  `{sandbox: workspace-write, approval: never}` profile, credentialed
  owner-only Codex home, and exact four-key environment already required by
  `CodexInstallationBinding.capture`. `codex.codex_home` contains exactly one
  owner-only non-symlink regular `auth.json` and its captured credential digest
  is non-null.
  The `pinned` member repeats those binding inputs with a distinct owner-only,
  empty `codex_home` (captured credential digest is null) and adds the
  owner-selected pinned permission profile. Both homes are absolute,
  non-symlink directories. `TMPDIR` is an absolute, existing, owner-only
  directory outside the writable project. Each normalized member records its
  freshly captured binding digest. No ambient PATH/profile or credentials are
  consulted after these exact inputs are captured.
- The static `RuntimeRequirementIndex`, its private bound view, and `grants` are
  keyed uniquely by `grant_selection_key`; only a bound-view/grant record maps
  that stable key to exactly one current `requirement_digest`. Each
  `OwnerRuntimeGrant` also contains authority `os_user_execution`, a positive
  `grant_generation`, and the current `policy_generation` and
  `config_generation`. Configuration alone never creates a grant.
- Add local provisioning command
  `lockstep owner provision-runtime --config PATH --project PATH --recipe NAME... --replace-grants PATH`.
  `--config` and `--replace-grants` each name an absolute, existing, regular,
  non-symlink UTF-8 JSON file no larger than 64 KiB and 512 KiB respectively;
  bytes are read once before validation. `--config` has no unknown keys and is
  exactly:
  ```json
  {"schema":"lockstep.runtime-provision-config/v1","codex":{"executable":"/absolute/executable","model":"explicit-model","cli_version":"explicit-version","permission_profile":{"sandbox":"workspace-write","approval":"never"},"codex_home":"/absolute/credentialed-owner-home","environment":{"PATH":"/bounded/path","LANG":"C.UTF-8","LC_ALL":"C.UTF-8","TMPDIR":"/owner-private-tmp"}},"pinned":{"executable":"/absolute/executable","model":"explicit-model","cli_version":"explicit-version","permission_profile":{"sandbox":"workspace-write","approval":"never"},"codex_home":"/absolute/empty-pinned-home","environment":{"PATH":"/bounded/path","LANG":"C.UTF-8","LC_ALL":"C.UTF-8","TMPDIR":"/owner-private-tmp"},"pinned_permission_profile":"owner-selected-profile"}}
  ```
  All shown strings are non-empty; paths are absolute; the two homes differ.
  `--replace-grants` is exactly a JSON array of zero to 4,096 unique lowercase
  64-hex `grant_selection_key` strings; order is normalized lexically. The CLI
  passes only parsed mappings/tuples to provisioning, never a JSON path.
  Add the product read-only command `lockstep owner list-runtime-requirements
  --project PATH --recipe NAME...`. It derives the same real authorized closure
  and emits canonical JSON `{schema:"lockstep.runtime-requirements/v1",
  project_identity, requirements:[{grant_selection_key,definition_digest,
  protected_descriptor_digest,runner_selector,required_capabilities,
  required_authorities,uses}]}`. Each `uses` element follows the exact bounded
  static-inventory schema above, so an owner can select opaque keys without test
  fixtures or handcrafted hashes.
  This is a non-interactive, explicit **complete replacement** operation, never
  a recipe-local merge: repeatable `--recipe` roots define the complete set of
  authorized DAGs represented by the new snapshot, and `--replace-grants`
  names the complete desired subset by canonical `grant_selection_key`. The
  one operation supplies/rederives both bindings and the entire grant set in one
  transaction. Any recipe or grant omitted from the invocation is revoked.
  Omission of `--replace-grants` is an error; an empty JSON array explicitly
  revokes every runtime grant. The command resolves and validates the project,
  both bindings, union selection-key inventory, and exact replacement set;
  unknown, duplicate, or out-of-inventory selection keys fail before
  replacement. Only records with an identical complete stable-key input tuple
  coalesce and aggregate their bounded, canonical `uses`; differing definition
  or protected-descriptor digests remain distinct. One computed key with
  conflicting tuple facts is a collision/integrity failure. The command
  then atomically replaces and file/parent-directory-fsyncs `snapshot.json`. No
  TTY or typed-digest ceremony is required.
- Equal normalized bindings plus equal replacement grant set is byte-for-byte
  idempotent and changes no generation. Any binding change increments the one
  global `config_generation`; provisioning internally adds the newly captured
  exact binding digest and assigned config generation to each selected stable
  key, rebuilds every current requirement digest, and deterministically reissues
  the complete retained replacement grant set under those new digests. The
  caller never supplies or predicts a requirement digest. No old-config grant
  remains. Any replacement-set change
  increments `policy_generation` and rebuilds every retained grant record with
  that current policy generation. Old-to-new predecessor matching is only by
  `grant_selection_key`: a selected key whose binding/config-derived requirement
  changes receives previous `grant_generation + 1`; a newly selected key,
  including one present but ungranted before the same binding bump, receives
  generation one; a selected unchanged key retains its generation. Removed keys
  are absent. Generations never decrease or wrap. This command is the sole
  provisioning path documented for v1. Direct editing is unsupported: malformed
  or internally inconsistent state fails closed, but the same OS owner is the
  trust boundary and a plain JSON snapshot cannot promise cryptographic
  detection of a deliberate, internally consistent direct rewrite.
- A command composition opens the snapshot once, captures both bindings once,
  and fails if their digests differ from the normalized snapshot. The same
  immutable `OwnerRuntimeSnapshot` is used by preflight and commitment.
  Commitment reopens the owner file only to prove the same snapshot digest and
  generations remain current; drift/revocation fails closed. All provisioning
  and every `resolve`/`commitment` use the same owner-state `snapshot.lock`
  advisory file lock. Provisioning holds it across read/validate, fresh
  generation computation, replacement, and both fsyncs; `commitment` holds it
  across final re-read/revalidation and `runner.ensure_started`. The existing
  crash-released exclusive `advisory_file_lock` is sufficient; concurrent
  provisioners therefore serialize and cannot lose or regress a generation.
- Before `blobs.put`, recipe capture, project snapshot capture, catalog insert,
  `EffectLedger.admit_start`, or native start, derive `RuntimeRequirement` for
  every managed/pinned descriptor in every root/direct/transitive authorized
  recipe file. Verify the exact selector binding and `OwnerRuntimeGrant`.
  Unknown selectors and unsafe/unknown/drifted configuration fail with zero
  start-side durable change. `OwnerRuntimeAuthority.preflight(index)` returns
  an immutable decision containing the snapshot digest/generations and exact
  requirements. Under the coordinator's existing admission serialization, and
  before the first start-side durable write, `decision.assert_current()` takes
  the owner lock, re-reads/revalidates that snapshot, and keeps the lock through
  the admission's first-write critical section. Drift causes rejection before
  any blob, bundle, project snapshot, catalog, runtime-input, watch, checkpoint,
  effect, or event fact is written.
- `RuntimeRequirementIndex` is deterministically derived, not stored in a new
  table: `RuntimeRequirementIndex.for_authorized_closure(...)` parses only the
  exact real authorized root/direct/transitive definition DAG and maps each
  descriptor to its static selection-key tuple. `index.bind(snapshot)` maps that
  inventory to current binding/config-derived requirement digests. Only equal
  complete tuples coalesce and aggregate canonical uses; conflicting facts under
  one computed key fail closed. Preflight uses the bound view before capture;
  after admission and on
  restart, the coordinator reconstructs the same selection key from the
  immutable recipe bundle referenced by the run catalog plus the bound project,
  resolves its current requirement digest through the owner index, and compares
  the request's exact binding facts. The index supplies only audit
  `logical_file`/`uses`. A mismatch cannot fall back to selector-only policy.
- At the existing coordinator commitment boundary, derive the current
  coordinate/input/deadline-bound `EffectGrant` only after reconstructing the
  descriptor's stable selection key, resolving its one current
  `requirement_digest`, and finding the exact grant mapped to both. Recheck the
  selection key, binding digest, requirement digest, policy/config/grant
  generations, and owner snapshot under the existing commitment guard immediately
  before spawning. A final revocation/generation/binding drift after resolution
  preserves any already persisted immutable grant/launch audit facts from the
  existing prepare/launch phases, but permits no spawn, running observation,
  result delivery, or native continuation; those facts cannot authorize a later
  launch after revocation. The distinct preflight-to-`assert_current` drift cut
  remains all-start-facts-absent because it occurs before the first write.
  Preflight never mints an `EffectGrant`.
- An `AcceptDescriptor` may pass preflight without an issued bearer token. It
  parks fail-closed at acceptance until `OwnerConsentAuthority` validates
  dynamic consent. Do not create a second publication authority.

### Run-drive watch v2 and migration metadata

- Modify `engine/src/lockstep/runtime/storage.py`: replace
  `effect_dispatch_watches` with `run_drive_watches(admission_seq INTEGER
  PRIMARY KEY AUTOINCREMENT, public_run_id UNIQUE NOT NULL FK,
  input_blob_sha256 NULL, input_blob_size NULL, admitted_at NOT NULL)`. SQLite
  assigns the immutable, never-reused sequence; it orders discovery only and is
  neither workflow state nor a durable recovery cursor. Enforce that digest and
  size are both null or both non-null. Add
  `runtime_schema_migrations(name PK, schema_version NOT NULL,
  after_public_run_id NULL, completed_at NULL, updated_at NOT NULL)` and the
  singleton `runtime_schema_epoch(singleton PK CHECK singleton=1, epoch NOT
  NULL)` row.
- Modify `engine/src/lockstep/runtime/effects/ledger.py`: rename the watch DTO
  and APIs to `RunDriveWatch(admission_seq: int, public_run_id: str,
  input_blob_sha256: str | None, input_blob_size: int | None, admitted_at:
  datetime)`. The sequence is positive; `public_run_id` is the canonical catalog
  ID; the blob fields are both null or a lowercase SHA-256 plus non-negative
  size not exceeding 64 MiB;
  and `admitted_at` is the stored UTC instant. Its APIs are
  `EffectLedger.max_run_drive_admission_seq() -> int | None`,
  `EffectLedger.list_run_drive_watches(*, after_admission_seq: int,
  high_water: int, limit: int) -> tuple[RunDriveWatch, ...]`, and
  `EffectLedger.acknowledge_run_drive_watch(public_run_id: str) -> None`.
  `list` requires `0 <= after_admission_seq <= high_water` and `1 <= limit <=
  128`, returns strictly increasing records in `(after_admission_seq,
  high_water]`, and `acknowledge` is idempotent for a canonical ID. The latter
  is the one atomic watch-delete storage operation. `admit_start` still
  atomically inserts immutable run binding, exact start-input reference, and one
  watch. External effect APIs, including `list_recovery_threads`, retain their
  attempt-only meaning.
- Create private `RuntimeSchemaMigrator` in the migration/storage boundary with
  `apply_run_drive_watch_page(*, expected_after_public_run_id: str | None,
  classified: tuple[LegacyRunDriveClassification, ...], exhausted: bool) ->
  MigrationProgress`. `LegacyRunDriveClassification` is exactly
  `(public_run_id: str, disposition: Literal["nonterminal", "terminal",
  "malformed"])`; every ID is a canonical catalog public ID. Records are at
  most 128, sorted by unique `public_run_id`, and every ID is strictly after the
  expected cursor when one exists. Callers supply already classified records
  only. This single transaction validates the
  expected cursor: `nonterminal` inserts one null-input watch; `terminal` and
  `malformed` advance only. `exhausted=True` durably completes this named
  migration in the same transaction; false never completes it.
  `MigrationProgress` is exactly `(after_public_run_id: str | None,
  completed: bool, inserted_public_run_ids: tuple[str, ...],
  malformed_public_run_ids: tuple[str, ...])`: both ID tuples are sorted,
  unique, bounded by 128, subsets of this page with their corresponding
  disposition; `after_public_run_id` is the last applied page ID (or the prior
  cursor for an empty page), and `completed == exhausted`. Applying to a
  completed migration or with a cursor mismatch fails closed; unique watch
  insertion and one-way completion make successful replay/reopen observations
  idempotent.
  `RuntimeStorage._v2_write_transaction()` is a private, non-nested context
  manager: it takes the shared schema fence, issues `BEGIN IMMEDIATE`, verifies
  epoch exactly 2 inside that transaction before yielding, rolls back on an
  exception, commits only on clean return, then releases the fence. It is the
  only epoch-checked v2 write boundary. These are private natural storage
  methods, not public/test activation points or generic hooks.
- A new admission watch has a non-null input reference. Recovery always reads
  the public native snapshot first: any valid checkpoint suppresses
  `ensure_started` even if the watch still retains that non-null reference.
  Only no-checkpoint plus non-null input may call existing idempotent
  `ensure_started`. A migrated null-input watch must never call
  `ensure_started` or read an input blob.
  Null input plus no valid checkpoint is an isolated integrity error: report the
  run blocked, do not synthesize empty input, reset state, or stop other runs.
- Keep the same watch at worker manual park, sealed external effect, delivered
  effect exposing Decision, managed running, and pending acceptance. Remove it
  only after native terminal is observed and `reconcile_consumed` proves no
  residual post-native-commit effect work. Removal is idempotent.
- The R2 schema upgrade is named `run-drive-watch-v2` and pages legacy catalog
  bindings in ascending `public_run_id`, 128 per migration transaction. For
  each binding after the recorded key, inspect the public native snapshot; in
  one SQLite transaction insert a null-input watch iff it is nonterminal and
  advance `after_public_run_id`. Terminal or malformed bindings advance the
  migration cursor and are reported, never re-armed/reset. A crash before the
  transaction repeats that binding; a crash after it resumes after that key.
  New admissions already have v2 watches, so keys inserted behind the cursor
  need no backfill. On exhaustion set `completed_at` durably and never scan the
  legacy catalog again.
- Fence the transition with `$LOCKSTEP_STATE_DIR/runtime-schema.lock` and an
  exclusive SQLite schema transaction. The upgrader verifies legacy epoch 1,
  creates/populates the v2 tables and epoch-2 singleton, and removes/renames the
  legacy watch table before releasing the lock. Every v2 command-side writer
  (admission, watch acknowledgement/removal, recovery repair, effect mutation,
  and consent mutation) acquires the schema fence shared with other v2 writers
  and verifies exact epoch 2 before its write transaction. Projection remains
  read-only. A b794 process that began before the exclusive transition either
  commits before the upgrader obtains exclusivity and is included by migration,
  or its transaction finishes against the removed/incompatible legacy schema
  and rolls back; it cannot commit a legacy watch after epoch 2. Mixed-version
  command writers are unsupported and fail closed. Do not add a migration
  candidate table.
- R2a is a **policy-free durable-protocol skeleton**, not a no-op and not a
  recovery-policy/pump implementation. It may install the DTO/DDL, epoch/fence transaction,
  high-water scalar query, bounded ordered page query, atomic watch deletion,
  and atomic application of caller-supplied classified migration pages. It may
  not inspect catalog/native state to classify a binding, choose/iterate pages,
  decide watch lifetime, perform cleanup, recover/drive a run, apply fairness or
  capacity deferral, or construct/activate an automatic pump. R2 owns all of
  those policies.
- This durable row is **owner-state schema-upgrade progress only**. It contains
  no workflow/native coordinate, pending kind, route, status, outcome, owner,
  effect phase, or grant. It is consulted only while upgrading b794 data. Once
  `completed_at` is set, ongoing and crash recovery correctness depends solely
  on `run_drive_watches` plus native/effect facts. No durable scheduler or
  workflow recovery cursor exists.
- Automatic and explicit recovery capture
  `high_water = MAX(admission_seq)` once at sweep start, then use bounded pages
  ordered by `admission_seq` and constrained to `admission_seq <= high_water`.
  Watches inserted after capture belong to the next sweep, so concurrent
  admissions cannot extend the current population. `limit` is accepted drive
  attempts, not the first N watch rows. A sweep scans
  past synchronously proven worker parks, terminal-clean rows, and isolated
  blocked rows until it accepts `limit` runnable drives or exhausts the captured
  sequence population. A process-local `after_admission_seq` may optimize a live
  pump and wraps at most once within that fixed high-water population; a fresh
  process begins at zero and must still reach a later runnable watch in one
  finite sweep. Parked/blocked/terminal-clean rows advance scan position without
  consuming attempt budget. A capacity-blocked row also consumes no accepted-
  attempt budget, but it is added once to a bounded process-local deferred set
  and the local iterator advances past it for the remainder of this fixed-high-
  water sweep. The sweep inspects later candidates once rather than busy-retrying
  the deferred row. Deferral does not durably advance eligibility: the next
  process-local sweep begins inclusively at the earliest deferred
  `admission_seq` (or the ordinary wrapped optimization point when none), with a
  fresh high-water; freeing capacity therefore makes that row selectable on the
  next sweep. The deferred set and next-start value are ephemeral and bounded by
  the captured population. Per-run errors are reported and scanning continues.

### Historical pre-simplification: whole-DAG plan and owner-state authoring transaction

> This detailed transaction/journal design is retained solely as historical
> evidence for the 2026-08-25 freeze. It is superseded for active authoring by
> the 2026-08-29 per-file contract stated above; do not implement or restore any
> of its journal, rollback, or automatic-recovery interfaces.

- Create `engine/src/lockstep/authoring_bundle.py` with immutable
  `SourceIdentity`, `DestinationImage`, and `ProjectCompilationBundle`.
  `ProjectCompilationBundle` contains child-first source roles, complete direct
  and transitive dependency closure, every recipe/manifest/source-map/generated
  output, an exact destination map, a read set, before images, and after images.
- The read set records every root/child workflow's exact bytes/SHA-256, resolved
  contained regular-file path, leaf `(device,inode,mode,size,mtime_ns)`, and
  ancestor identities. The ordinary compile write set contains only every
  role's recipe, dependency manifest, source map, and generated files. Existing
  workflow sources never enter it. Template install adds staged sources to the
  write set under an all-destinations-absent precondition.
- Reuse existing `RecipeLimits`: at most 256 source/destination records and
  4 MiB aggregate bytes independently for the read set, before-image bytes, and
  after-image bytes. Reject before acquiring a publication journal if any bound
  is exceeded.
- Planning is pure and completes recursive reads exactly once plus parse,
  semantic, compile, strict ingress, containment, symlink/non-regular/ancestor,
  collision-across-roots, and aggregate checks before the first project write.
  `compile_project_source`, `write_compilation`, `canonical_match`,
  `check_recipe`, and `diff_recipe` consume the same bundle.
- Create `engine/src/lockstep/authoring_publisher.py` with
  `AuthoringPublisher.publish(bundle)` and `recover(project)`. Store its lock,
  journal, and bounded before-image blobs at
  `$LOCKSTEP_STATE_DIR/authoring/<sha256(resolved-project-identity)>/`. The
  trusted journal schema `lockstep.authoring-transaction/v1` binds the resolved
  project path and identity, operation id, complete read/write sets, exact
  before bytes/mode/absence/ancestor identity, exact after bytes digest/mode,
  and replacement progress. Owner files are regular non-symlinks at `0600` in
  verified `0700` directories; journal and namespace changes are file- and
  parent-directory-fsynced.
- Under the authoring lock, recover an active journal to all-old or fail closed,
  then validate the complete read set before the first replacement. Immediately
  before **each** destination mutation, reopen its verified parent by directory
  descriptor and revalidate that destination's recorded leaf-or-absence and
  every ancestor identity. For recorded absence, publish the same-directory
  staged inode with an atomic exclusive/no-clobber operation; `EEXIST` is a
  foreign creation and must never be overwritten. For an existing destination,
  compare its exact leaf identity immediately before an identity-checked
  same-directory replacement. A mismatch preserves the foreign path, rolls back
  only destinations already written by this transaction, and retains trusted
  journal evidence until rollback durability is proven.
- The filesystem threat model is crash/fault cuts plus cooperating Lockstep
  writers serialized by the owner-state authoring lock. As elsewhere in the
  local MVP, a malicious concurrent process running as the same OS user is not
  isolated; the implementation must not claim a portable path compare-and-swap
  against that excluded actor. Within the stated boundary, parent-dir handles,
  per-mutation identity checks, no-clobber creation, and same-directory atomic
  replacement close every supported race.
- Revalidate the complete read set after every replacement and once more after
  all after-images/modes are durable. That final successful read-set validation
  is the transaction's commit linearization point; only then may the journal be
  durably removed. A source edit causes restoration of transaction-written
  outputs while preserving the source edit. A destination matching neither
  before nor planned-after identity causes fail-closed recovery that preserves
  the foreign file and trusted evidence.
- On any raised publish failure restore existing destinations to exact bytes and
  mode, remove only paths whose recorded before image is absence, fsync each
  file and affected parent namespace, and keep the journal until restoration is
  durable. Success requires every destination to match its after identity before
  durable journal removal.
- Modify `engine/src/lockstep/templates/__init__.py` so install only stages
  template sources and invokes the same bundle planner/publisher. Delete its
  template-specific journal/replacement loop. It retains the stricter
  all-destinations-absent collision policy.
- CLI and MCP authoring calls pass `state_dir()` into the publisher. Compile,
  install, check, diff, and canonical-match first recover an active transaction;
  read-only commands never report a mixed bundle. Do not use Task 10
  `ProjectPublisher`; sharing a low-level authority-free fsync helper is allowed.

### Installed contract

- Modify active `README.md`, `docs/DESIGN.md`, `skills/lockstep/SKILL.md`,
  `skills/lockstep-author/SKILL.md`, `recipes/examples/**`, plugin manifests,
  and package-facing examples to describe Workflow DSL, direct child calls,
  parallel, Decision, acceptance, explicit observe/command construction, the
  two-binding owner provisioning command, and recovery v2.
- Delete `engine/src/lockstep/runtime/runners.py` and
  `engine/tests/test_runners.py` after proving no production importer remains.
- Rename the pre-v1 estimate field `peak_parallel_subcalls` to
  `peak_parallel_child_calls` in `workflow/estimate.py`, CLI output, tests, and
  active guidance. Version remains `0.1.0`; no compatibility alias is retained.
- Historical factual `CHANGELOG.md` entries and the explicitly superseded parity
  spec are excluded from active scans and remain unchanged.

## Exact RED gates

Gate A0/A-schema, B0, and B-schema are added against b794. Gate A later REDs
are added against their reviewed R1a/R1b baseline; B1 is added against reviewed
R2a after the R1 B0-preservation gate. Gates C/D use their explicitly ordered
preceding feature ranges. A named RED must fail for its stated oracle, not for
fixture/import/setup errors; named R1a/R2a safety/protocol invariants are
expected GREEN. An unexpected result or wrong failure stops the unit and
triggers re-review. RED commits may change tests and the frozen design document
only; no production code.

### Gate A — staged R1 contract and behavior gates

Create `engine/tests/runtime/test_projection_capability.py` and
`engine/tests/runtime/test_public_composition.py`; extend public CLI/MCP and
managed-effect integration tests as needed.

**A0 — b794 behavioral REDs.** Add only A1-A3 that reach real current paths:
direct `Engine(state, recipes)` construction; each separately proven cold CLI
read (`status`, `wait`, `history`, `events`) against a genuinely recoverable
unrelated run; and each real cold MCP read after singleton reset. Their final
facts show construction of active command resources/a changed recoverable run,
not a future-factory absence. A parameter that is byte-identical on b794 is a
GREEN baseline control, not a forced RED. A4-A17 are not b794 behavior tests.

**A-schema — b794 independently reachable missing-surface REDs.** Assert only
one directly reachable absent surface per test: explicit `Engine.observe` and
`Engine.command` attributes on the existing class; the projection module; the
`LockstepCommandService` name in the existing service module; the released-
composition module; the owner-policy module; and the root owner CLI verb. An
absent module is one RED regardless of how many future types it will export;
do not multiply or hide type/export/method/field assertions behind its
`ModuleNotFoundError`. Exact types and export names, field shape, closedness,
parser details, parsing, value, bounds, ordering, digest-vector, listing,
normalization, and snapshot-fact assertions belong to ordered R1a microcycles
after their immediately required surface exists. These tests contain no
post-absence behavioral body, freeze no unspecified signature/DTO constructor,
and accept no public injection seam.

**R1a — policy-free execution-inert skeleton (GREEN).** It may add
`RuntimeReadResources`, passive `RuntimeProjection`, explicit engine factories,
separate CLI/MCP lazy handles, inert command construction, structural
`ReleasedRunnerComposition(codex, pinned)`, the five owner-policy values/pure
hashes/static index/private bound view, bounded JSON ingress, static read-only
listing, and filesystem/config/replacement validation. R1a is a sequence of
one-real-oracle RED→minimal-GREEN microcycles: after introducing each immediately
required surface, add the next independently reachable assertion for exact
projection/composition/owner-policy types and export names, then their named
methods and field shape/closedness, A4 projection isolation from poisoned
managed state, parser grammar, JSON value/path/bounds/order validation,
inventory/listing canonicalization, known digest vectors, and snapshot
normalization. Never batch future behavior behind the first missing surface.
Valid provisioning after that validation returns only
`runtime provisioning policy is unavailable` with no owner-state write; a
managed/pinned start before its first write returns only
`runtime execution policy is unavailable`. These are temporary reviewed
baselines, not new exception/schema/installed contracts.

R1a must not create/mutate the snapshot, assign generations, capture providers
at construction, grant authority, start/inspect/quiesce a provider, spawn,
deliver, continue native state, activate recovery/pump, make a preflight
admission/currentness/commitment decision, write launch/grant audit, add a
registry/hook/alternate ingress/table, or consult ambient authority. Projection
ignores absent/poisoned owner snapshot. A-schema, A1-A4 passive controls, A5
structural composition, static closure/listing, invalid ingress/config controls,
and the two write-free unavailable outcomes are GREEN; B0 retains every reviewed
RED parameter and B-schema remains RED.

**R1b-P — snapshot provisioning RED then GREEN.** Carry forward, never relabel
as RED, the R1a-green invalid A8 home/auth/executable/pinned/TMPDIR/replacement
matrix (including codex-home symlink and every group/other mode bit) and invalid
A13 ingress. Add genuine REDs for valid captured dual bindings plus real static
inventory persisting the first snapshot; A13 valid complete replacement,
idempotence, coalesced uses/omission/no-merge; A14 rekey/reissue; and A17 real
independent provisioners/crash/kernel-lock release. Implement only snapshot
store/lock/capture/generation/predecessor/atomic-replace/fsync path.

**R1b-A0 — static admission RED then GREEN.** Add exactly two positive REDs:
fully provisioned and exactly granted real `codex` and `pinned` closures must
pass static admission and reach a durable pending pre-launch park without
provider resolution, spawn, running observation, delivery, native
continuation, or launch/grant audit. The reviewed R1a inert policy error is the
honest missing positive branch for these fixtures because each fixture contains
a real normalized snapshot and its exact current grant. Carry A8
ungranted/configuration-only and binding-drift zero-write cases, A9 complete
root/direct/transitive inventory and omitted-grandchild rejection, A10
acceptance without bearer, A11 configuration-not-grant, and A12
projection-only behavior as GREEN safety controls; do not force them RED.
Implement only immutable snapshot open/capture, bound full-DAG inventory, exact
static grant preflight, and the immutable admission decision. No currentness
guard, dynamic provider resolution, or spawn belongs to A0.

**R1b-A1 — first-write currentness RED then GREEN.** Only after both A0 positive
REDs are GREEN, add A16. Wrap the existing real
`service.plan_authorized_start` only as a deterministic race barrier: call the
original helper to completion, signal that genuine static preflight succeeded,
use the supported provisioning command in another thread/process to revoke or
reconfigure the snapshot, then release start. The wrapper must not replace or
forge the authority, index, snapshot, decision, or durable stores. The RED
oracle is rejection with every start-side blob, bundle, project snapshot,
catalog, runtime-input, watch, checkpoint, effect, and event fact absent.
Implement `decision.assert_current()` as the natural owner-lock guard and hold
the same owner lock through the existing admission first-write critical
section. Admitted managed work still parks at the pre-launch boundary; A1 adds
no R1b-E resolve/commitment/provider behavior. Its post-persist recovery-failure
control proves the committed run remains durable after activation rollback; it
does not invoke managed restart reconstruction, which belongs exclusively to
R1b-E2/A15.

**R1b-E0 — public composition/commitment RED then GREEN.** Carry
`reviewed-change` and `parallel-review` as GREEN public-command controls proving
their current compiled scope-only graphs create no runtime requirement, grant,
provider request, launch audit, or spawn. Add and independently review only A5
against a separate real public-
command authorized closure containing a protected managed Codex descriptor,
preserving the exact adapter binding digest through the static requirement,
bound requirement digest, dynamic `EffectGrant`, `EffectRequest`, provider
preparation, and durable launch commitment. Implement the smallest production
composition, immutable-bundle requirement reconstruction, owner authority
resolution/commitment, and closed `codex`/`pinned` adapter binding needed to make
A5 GREEN, including the shared-lock commitment recheck through `ensure_started`.
The same E0 composition must be installable on the first protected start even
when the reusable command capability was already activated by an ordinary
command; ordinary activation is not a revocation and must not poison later
protected work. The installation is serialized with command activation and may
not expose a partially replaced runner/authority pair to the pump.
No A6 GREEN lifecycle control, A7 RED, A15 RED, or after-resolve A16 RED may be
frozen while A5 still fails at the static-admission park.

Before E0 production, land and independently review one tests/design-only E0
gate-transition commit. Retire only A0's temporary public-start park/cache
outcomes: replace the positive `codex`/`pinned` park tests with real static
planning/preflight proofs of the exact `RuntimeAdmissionDecision`, bound
requirement identity, selector binding, generations/grant, and zero start-side
writes; remove the two restart/repeated-classification tests whose sole contract
is preservation of the temporary park. Preserve every negative A0 authority,
inventory, bearer-separation, binding-drift, snapshot-integrity, and write-free
control. Preserve all A1 currentness/lock-order/committed-run invariants, while
removing only wording that treats the temporary park as a final result. Add the
three A5 cases to the evolved Gate A. E0 review later found and retired one
additional cache-only shutdown control that preserved no invariant beyond the
adjacent pump-join test. The corrected pre-production baseline is therefore
exactly `219` collected: `218` GREEN and the sole A5 RED at the missing durable
owner-current launch commitment. Historical `219 passed` remains evidence for
the completed A1 stage, not a permanent count. No E0 production change may
precede independent approval of this gate transition.

**R1b-E1 — selector lifecycle control plus RED then GREEN.** Only after reviewed
A5 GREEN, add A6 production Codex lifecycle as a carried GREEN control and A7
pinned lifecycle/verify/credential-free-home/exact-profile-argv as the sole RED.
Independent reachability on `23f82a7` proved that A6 already completes the real
Codex process, PASS/result/rollover, durable delivery, and native completion;
forcing it RED would invent a defect. A7 uses `Engine.command` and a real
authorized immutable `kind=verify` closure, reaches exact grant/workspace/binding
and durable `prepared`, then fails solely because the pinned strategy accepts
only `kind=pinned`. GREEN must admit the closed semantic pair required by the
public contract without rewriting the request/grant/ledger kind and without
accepting arbitrary effect kinds. The final A7 oracle remains exact owner-selected
profile argv, credential-free pinned home, PASS with no result/snapshot
publication, durable delivery, and native completion.

**R1b-E2 — restart reconstruction RED then GREEN.** After the public lifecycle
path exists, add A15 using a real admitted run, close/reopen, immutable catalog
bundle, and production composition. Independent reachability proved that the
first honest baseline symptom of missing restart composition reconstruction is
the fresh coordinator's exact durable-binding lookup failure in
`_recover_effect_batch → _runner_for_binding`: admission, grant/commitment,
durable `running`, real spawn, catalog binding, and immutable bundle have already
succeeded, but no runner with the ledger's exact binding digest exists after
reopen. This exact `ProviderContractViolation`/digest `KeyError` is therefore the
sole permitted RED, not a fixture or selector lookup failure. GREEN reconstructs
the requirement only from the catalog-bound immutable bundle, verifies the
unchanged current owner snapshot/grant/bindings, atomically installs production
composition before reconciliation, adopts the existing process without a second
spawn, and reaches PASS/result/rollover, durable delivery, and native completion.
No selector fallback, live-recipe read, regrant, injected context, owner drift,
or R2 recovery policy is permitted.

E2 GREEN is complete. A focused `RuntimeExecutionRecovery` validates every
selected bounded recovery page against its catalog-bound immutable bundle and
the current owner snapshot, grants, project boundary, provider kind, and exact
durable runner binding. One owner context/composition is installed under the
activation lock before reconciliation; later equal pages are revalidated
without reinstall and differing pages fail closed. Automatic and explicit
recovery share the exact cursor/limit page, retain the global
`activation → admission` lock order, and reject per-run overflow at `128 + 1`.
A15 adopts the existing process with zero fresh spawns and completes after the
live recipe is deleted. Independent final review found no Critical, Important,
or Minor findings. Gate A is `256 passed`; full non-R2 is `1205 passed, 1
skipped` with the unchanged warning; frozen R2 remains exactly `29` failures.
E3/A16 is next.

**R1b-E3 — after-resolve drift GREEN carry-forward.** Independent production
reachability found a staging plan defect: E0 already installed the shared owner
snapshot commitment guard, so the exact future A16 oracle is GREEN on E2 without
an E3 production change. Freeze A16 only as a real carried control. Its barrier
must call the original production resolve and pause after that exact successful
grant/request resolution but before entering the original commitment context.
Drift only through supported provisioning. The unchanged production commitment
must reject the changed snapshot before `ensure_started`, preserving the exact
durable `launching` request/grant/workspace/launch-commitment audit, native
snapshot and pending coordinate, with zero spawn, result, delivery, or native
continuation. Never invent a RED by bypassing or replacing authority behavior.

E3 is complete as a tests/design-only carried GREEN control. The barrier calls
the original final resolve, correlates its exact authority/intent/grant/request,
then pauses before entering the original commitment context. Supported CLI
reprovision changes the Codex binding and advances configuration/grant
generations while retaining the same selection key and policy generation. The
unchanged E0 commitment guard rejects the new owner snapshot before spawn;
the durable `launching` audit and passive native snapshot/pending coordinate
remain byte-for-value equal, with zero provider markers, result, delivery, or
continuation. Independent review found no Critical, Important, or Minor
findings. Gate A is `258 passed`; full non-R2 is `1206 passed, 1 skipped` with
the unchanged warning; frozen R2 remains exactly `29` failures. R1b-E0–E3 is
complete; the next step is the remaining Task 12 sequence and combined R1
review, not Task 13+ reassessment.

All R1b-E tests wrap real seams only; no injected runner/authority/index/bundle/
fault API. Packaged-template behavior may become authority-bearing only under a
separately reviewed Task 12C product-contract RED.

| Oracle | First honest stage | GREEN owner |
| --- | --- | --- |
| A1-A3 | A0 | R1a |
| A4 projection isolation from poisoned managed state | R1a one-oracle microcycle | R1a |
| A5 composition module / type and closed pair | A-schema / R1a microcycles | R1a |
| A5 packaged-template scope-only no-spawn/no-grant controls | R1b-E test freeze | carried GREEN |
| A5 real protected public-command digest/request chain | R1b-E RED | R1b-E |
| A6-A7 | R1b-E1 A6 carried GREEN + sole A7 RED after reviewed E0 GREEN | R1b-E1 |
| A15 restart reconstruction | R1b-E2 RED after reviewed public lifecycle GREEN | R1b-E2 |
| A16 after resolve | R1b-E3 carried GREEN after reviewed real resolution/launch preparation | R1b-E3 |
| A8/A13 invalid validation | R1a one-oracle microcycles | R1a, carried GREEN |
| Granted real codex/pinned static admission | R1b-A0 RED | R1b-A0 |
| A8 command drift/ungranted, A9-A12 | R1b-A0 carried/new GREEN controls | R1a / R1b-A0 |
| A16 after successful real preflight | R1b-A1 RED after A0 GREEN | R1b-A1 |
| A13 valid replacement, A14, A17 | R1b-P RED | R1b-P |

At each R1a/R1b checkpoint run the focused Gate A command plus affected
provider/owner-state/bundle/crash controls; completed cycles are GREEN,
not-yet-owned behavior is absent or fails only at its inert boundary, B0 retains
its reviewed RED per parameter, and unrelated tests are GREEN. A RED commit is
tests plus this design only. Stop on collection/import/fixture failure, early
gate, changed durable oracle, unexpected GREEN, injection seam, ambient
authority, second snapshot writer, registry, or permission leak.

### Gate B — staged v2 contract and behavior gates

The current b794 service can demonstrate most failures, but crash, pagination,
and epoch-race behavior needs a minimal v2 test surface. Therefore no claim of
“all B behavioral REDs before R2 production” is valid. Create
`engine/tests/runtime/test_run_drive_watch_v2.py` and
`engine/tests/runtime/test_run_drive_migration.py`; extend crash tests. Every
test must execute real durable/service behavior and fail at its final state or
trace oracle. Source-string checks, API-absence checks, constant-membership
assertions, tautological early assertions, and a bare-catalog migration fixture
without a real public native snapshot are prohibited. Persistence and ordering
oracles use real SQLite catalog/watch/effect stores and real native snapshots;
only clocks, provider adapters, capacity decisions, and deterministic pump
scheduling may be faked.

**B0 — b794 behavioral REDs (tests and freeze document only).** These fail
independently at the stated oracle on b794:

1. `test_recovery_consumes_rowless_decision_after_manual_delivery_crash`: use a
   real manual-to-Decision state (or minimal real service state) with no
   dispatch watch/nonterminal effect row; recovery must resume exactly once and
   select the branch without a Decision ledger row.
2. `test_drive_watch_survives_every_nonterminal_park[...]`: use the actual old
   `effect_dispatch_watches` row through worker manual, sealed external,
   delivered-to-Decision via predecessor delivery, and managed running; it
   remains at every named nonterminal park. Pending acceptance is deliberately
   not synthesized here and is the mandatory B0.5 case below.
3. `test_start_watch_replays_only_before_first_checkpoint[non-null]`: an old
   non-null admission watch reads/starts once without checkpoint and neither
   reads nor starts with a valid checkpoint.
4. `test_watch_does_not_authorize_blocked_runner`: explicit and automatic
   recovery scan past a managed request whose authority/runner remains denied;
   snapshot its request/grant/launch facts and prove zero new protected action,
   then drive a later runnable rowless Decision.
5. `test_watch_is_not_removed_at_nonterminal_park`: prove the old watch is
   prematurely absent at a real manual nonterminal park. This B0 case does not
   claim terminal/residue cleanup; its full terminal orchestration belongs to
   B1 after R2a below.
6. `test_fresh_driver_reaches_decision_after_128_worker_parks`: fresh drivers
   over 128 real durable worker parks reach a later pending rowless Decision in
   one finite sweep with no durable scheduler fact.
7. `test_explicit_and_automatic_recovery_fairness`: a real durable population,
   with fake clock/provider/capacity/deterministic scheduling ports only as
   needed, records both public explicit and automatic scan/drive traces past
   foreign, parked, and blocked rows to a later valid row.
8. `test_capacity_deferral_advances_current_sweep_and_preserves_next_eligibility[explicit|automatic]`:
   a capacity-blocked managed row precedes a rowless Decision; the Decision
   advances this sweep without consuming attempt budget, the earliest deferred
   row is first next sweep after capacity frees, and SQLite facts are unchanged.
9. `test_b794_acknowledged_state_backfills_null_input_watch`: make an actual b794
   delivered-predecessor/pending-Decision/no-watch state, close/reopen it, and
   require branch progress without reset. Terminal or malformed native bindings
   advance migration only and are never re-armed.
10. `test_repeated_recovery_is_idempotent`: use that recoverable rowless Decision
    state; first recovery makes exactly one expected transition and the second
    changes no normalized native/effect/watch/catalog/event/result fact.

**B0.4 — native child→parent lineage prerequisite (reviewed RED→GREEN before
B0.5).** The real B0.5 feasibility probe must first reach owner consent. It
currently fails closed earlier because an exact child producer checkpoint
cannot be related to a later pending parent acceptance. Freeze a real
direct-child→later-parent regression plus obsolete-fork, sibling-namespace,
foreign-thread, ambiguous-occurrence, missing-anchor, and bounded-history
fail-closed controls. Then make only the native runtime/adapter lineage boundary
GREEN with a bounded traversal of public parent-config edges and exact completed
subgraph snapshots. Do not add a lineage transcript, parse namespace strings,
read native SQLite tables, special-case acceptance, remove the coordinator
ancestry predicate, trust durable artifact equality by itself, or touch R2 watch
policy. Independently review tests/design before production and the complete
RED→GREEN range before returning to B0.5.

Stage B0.4 in two reachable microcycles because the existing native port carries
only one namespace and cannot express a cross-namespace query. B0.4a freezes and
makes GREEN only the explicit ancestor/descendant checkpoint-pair signature and
wiring while retaining fail-closed cross-namespace behavior. B0.4b then freezes
the real live/restart positives and deterministic adversarial topology controls
against that reachable port, followed by the bounded traversal GREEN. Do not
hide later behavior behind a malformed call or merge traversal policy into the
port-surface step.

**Completed 2026-08-27.** B0.4a tests-only `7078588` and neutral surface GREEN
`3d1c282` name both exact checkpoint pairs while preserving the old
same-namespace behavior. B0.4b tests-only `674fe69` freezes real live/restart
child→parent positives, exact identity mutations, real sibling and obsolete
forks, duplicate source ambiguity, missing completed-subgraph bridge, and one
aggregate traversal ceiling. GREEN `bc83324` requires unique exact source
lineage in `GraphRuntime` and performs only bounded public parent-config plus
exact completed-subgraph traversal in the yamlgraph adapter. Independent
architecture and threat-model reviews are PASS with C/I/M zero. Focused lineage
and adapter controls are `41 passed`; architecture is `21 passed` with the one
unchanged reviewed warning; full non-R2 produced `1221 passed, 1 skipped` plus
two sandbox-only nested-`uv` failures that both pass outside the sandbox; frozen
R2 remains exactly `29 failed`. The real B0.5 probe now passes owner-consent
preview/issue and restart facts and fails only at `watch == ()` instead of the
required retained watch. B0.4 is complete; B0.5 tests/design freeze is next.

**B0.5 — acceptance-lifetime RED before any R2 policy behavior (tests and
freeze document only).** Extract one shared fixture from the real child restart
path with real durable child artifact, producer result lineage, registry
materialization, owner consent, and pending native acceptance. It must prove
that setup, then fail only because the old durable watch was acknowledged early.
`FakeRuntime` or a synthesized native snapshot is forbidden. This closes the
acceptance member of the final watch-lifetime requirement without inventing a
test-only acceptance ingress.

**B0.5 RED frozen 2026-08-27.**
`test_watch_survives_real_child_artifact_until_pending_acceptance_after_restart`
uses the shared compiled managed-child fixture also exercised by the native
child restart integration test. Both initial admission and reopen use normal
`Engine.command`, a compiler-authorized immutable closure, production owner
snapshot provisioning, and the real owner authority/composition boundary; the
only substituted component is an exact-owner-binding fake provider adapter. A
read-only diagnostic observes the current old-watch acknowledgement from
`(run_id,)` to `()` while a managed interrupt is pending, but acknowledgement
is not a precondition: the test must become GREEN when that call is absent. The
test proves a real workspace rollover, delivered producer lineage, artifact
registry bytes, pending native acceptance, previewed/issued owner consent, and
all of those durable facts after close/reopen. Its only failing assertion is
the final lifetime oracle: the reopened watch IDs are actually `()` but must be
`(run_id,)`. The paired focused integration control remains GREEN.

**B-schema — independently reachable missing-surface REDs (tests and freeze
document only).** On b794, assert only an oracle that can actually be reached
without a preceding new surface: exact `RunDriveWatch`,
`LegacyRunDriveClassification`, and `MigrationProgress` names and field shape;
watch/migration/epoch table columns/types, PK/unique/FK/check constraints,
immutable `admission_seq`, singleton epoch/check constraint, and exact
five-column no-scheduler metadata shape; exact ledger high-water/page/ack and
`apply_run_drive_watch_page(..., exhausted: bool)` signatures; and presence of
the private non-nested epoch-2 transaction context boundary. Each direct
contract oracle is independent: a missing b794 name may fail that one name
contract, but no common callable/type gate may be used to represent a later
value rule. These failures establish only missing surface, not B0/B1 behavior.

**R2a — reviewed policy-free durable-protocol skeleton.** Implement only the
exact DTO/DDL and private natural boundaries frozen above:
`_v2_write_transaction()`, `max_run_drive_admission_seq()`,
`list_run_drive_watches(...)`, `acknowledge_run_drive_watch(...)`, and
`apply_run_drive_watch_page(...)`. Permitted effects are the epoch-checked
transaction, immutable high-water/page reads, atomic watch deletion, and atomic
application of caller-supplied classified migration records/cursor. Forbidden
effects are classification, page iteration, watch-lifetime/cleanup decisions,
recovery drive, fairness/capacity policy, and pump construction/activation.
Make B-schema and the R2a protocol invariants green, preserve B0 RED, and
independently review that absence of policy.

**R2a protocol RED→GREEN microcycles.** Once the immediately preceding minimal
DTO, signature, or transaction surface is GREEN, add exactly one behavioral
protocol RED which invokes that real surface and reaches its own oracle; then
make only the minimal policy-free storage change GREEN before adding the next
case. This is where, rather than on b794, the final required
`LegacyRunDriveClassification` acceptance/rejection and canonical ID/order/
bound/cursor rules are tested; likewise `MigrationProgress` accepted values,
sorted/unique/disjoint/bounded result IDs, cursor relationship, and strict
boolean/completed==exhausted semantics. Apply the same sequence to epoch
transaction behavior, atomic/idempotent acknowledgement, page application,
null-input insertion, terminal/malformed advance-only, cursor replay, and
completion. No fake DTO, shared missing-type gate, or placeholder behavioral
assertion is permitted just to retain a RED count.

For B1 driver-facing REDs only, R2a supplies the private inert boundary
`RecoveryDriver._drive_run_watch(watch: RunDriveWatch) -> bool`; it returns
`False` and performs no native/effect/write/pump action. It is not a public API
and cannot be activated except by the command driver's existing internal call
path. R2 replaces that inert body with policy; it does not add another driver,
hook, or authority.

There is no generic fault injector and no CLI, MCP, environment, recipe, or
snapshot-controlled fault activation. Tests monkeypatch private natural calls
only: raise before the real `acknowledge_run_drive_watch` call for pre-commit;
call it then raise for commit-before-ack; call one real
`apply_run_drive_watch_page` then raise for page-commit; or wrap the real
`max_run_drive_admission_seq`, insert with a second connection, then return the
captured value for high-water concurrency.

**R2a protocol GREEN invariants.** The completed microcycles run against the
reviewed skeleton and must pass: exact DTO/DDL and value validation; epoch
transaction semantics; atomic, idempotent `acknowledge_run_drive_watch` storage
behavior including the post-commit crash observation; atomic page apply with
cursor, `exhausted`, completion, null-input insertion, terminal/malformed skip,
and progress value semantics; bounded high-water/page query ordering; and
metadata shape plus no metadata access by the completed inert driver/protocol
path. Writers already routed through `_v2_write_transaction()` reject non-2
epochs write-free. These are GREEN safety/protocol checks, not dishonest b794
behavioral REDs.

**R2a.1 — neutral driver reachability correction.** Independent B1 feasibility
review found that items 13 and 16 would otherwise both fail at the same missing
command→driver call instead of reaching their page and high-water oracles.
Before freezing those tests, add exactly one private method to the sole
command-owned `RecoveryDriver`:
`_sweep_run_drive_watches(*, project_identity: str | None, limit: int) ->
tuple[str, ...]`. Its neutral implementation returns `()` and performs no SQL,
native, effect, write, classification, iteration, cursor, pump, or mutable-state
action. Automatic recovery calls it exactly once with `None` and the existing
bounded limit under the existing recovery lock; explicit recovery calls it
exactly once with the resolved project identity and requested limit under its
existing locks. Explicit recovery incorporates future returned exact accepted
run IDs into its existing `recovered`/`count` result; automatic recovery may
discard them. No relative ordering against the legacy recovery path is frozen.
Projection/status/wait do not reach it, no wrapper or second driver is added,
and the legacy start-watch path may coexist only while the sweep is inert.

R2a.1 is a reviewed exception to the earlier all-B1-before-R2-production order:
freeze its exact private surface RED, make only inert behavior GREEN, then
freeze command reachability RED and route only the neutral call GREEN. Re-run
the complete R2a and frozen B0/B0.5 gates. No B1 behavior may be implemented in
this correction.

**B1 — missing R2 driver/policy oracles (tests and freeze document only).** Each
RED uses real v2 rows, real native snapshots, two SQLite connections where
relevant, reaches the inert driver or a real R2a storage boundary, and fails
only because the R2 policy integration is absent. Already-GREEN controls are
called out explicitly and are not counted as REDs:

11. Complete `test_start_watch_replays_only_before_first_checkpoint` with a real
    null-input watch through `_drive_run_watch`: it snapshots only, never reads
    a blob/starts, and a null/no-checkpoint integrity case remains blocked.
12. `test_terminal_removal_crash_cuts`: terminal cleanup policy must retain a
    watch through native terminal plus `reconcile_consumed` busy/residue, select
    the real atomic delete only after consumed cleanup completes, and converge
    through the pre-delete crash cut; the already-GREEN post-commit storage
    branch is not reclassified as RED.
13. `test_backfill_progress_survives_restart_past_terminal_prefix`: 129+ real
    terminal/malformed legacy bindings precede the actual stranded Decision;
    the missing driver must classify real snapshots, iterate pages/restart after
    every real page apply, and reach exactly one null-input Decision watch.
14. `test_completed_backfill_never_rescans_or_rearms_terminal_runs` is deferred
    until item 13 is GREEN and has produced a real driver completion. Then
    reopen repeatedly with a migration-inspection spy; no scan, rearm, or
    row/byte change occurs. If the complete oracle is already GREEN, retain it
    as a control rather than manufacturing a RED. Protocol-seeded completion is
    insufficient for this driver-integration oracle.
15. Retain `test_migration_metadata_is_not_scheduler_state` as a GREEN R2a.1
    control rather than manufacturing a RED from unrelated missing drive
    behavior. Trace real explicit/automatic neutral recovery and assert zero
    migration-metadata SELECT/UPDATE and an unchanged row. Extend/rerun the same
    control after item 13 supplies a real driver completion.
16. `test_sweep_high_water_excludes_concurrent_admissions`: with >128 parks and
    an original later Decision, wrap `max_run_drive_admission_seq`, admit from a
    second connection after captured high-water, then use the R2 driver; first
    sweep excludes newer sequences and reaches original work; next sweep sees
    the additions.
17. Split epoch transition from writer routing. First freeze
    `test_epoch_one_rejects_every_v2_command_writer_write_free`: parameterize v2
    admission, watch deletion, recovery repair, effect mutation, and consent
    mutation at epoch 1 and require write-free rejection; the storage calls
    already routed in R2a remain GREEN controls while remaining command writers
    are independent RED nodeids. "Every" is evaluated by independent production
    transaction owner, not by one representative method per label: admission is
    the atomic `admit_start` transaction; watch deletion covers the command-side
    legacy acknowledgement plus the already-fenced v2 acknowledgement control;
    recovery repair covers the already-fenced migration-page control; effect
    mutation covers ledger prepare, the common ledger transition, effect runtime
    snapshot facts, and lease acquire/release; consent mutation covers issue,
    redeem, revoke, and the publication commitment transaction which must reject
    before yielding authority. Catalog and run-start writes remain covered inside
    atomic admission because production does not invoke them as independent
    command transactions. The matrix freezes the SQLite family and logical rows
    after all valid semantic fixtures and the schema-lock file are prepared; it
    does not claim that an entire public start leaves the owner-state tree
    byte-identical, because immutable bundle/blob/snapshot admission legitimately
    precedes its SQL transaction. A top-level command reachability oracle is
    frozen separately before transition wiring. The later two-process transition
    test requires
    a real pre-open production owner that does not yet exist; a test-built raw
    DDL upgrader is forbidden. Immediately before that test, freeze the exact
    private `@classmethod` surface
    `RuntimeSchemaMigrator.transition_legacy_to_v2(Path) -> None`, add only a
    fail-closed staged implementation that performs no I/O and raises
    `NotImplementedError`, then freeze the real behavioral RED.
    That RED coordinates real connections/processes around the private
    transition: a legacy transaction either commits before and migrates or
    rolls back/fails after epoch 2. Implement the transition atomically while it
    remains unwired. Then freeze a separate real command pre-open reachability
    RED against a legacy database and only then wire the implemented transition
    before `SQLiteStore`; add unchanged empty/v2 command-open controls and prove
    projection never calls it. Exact legacy/valid-v2 states return normally
    after proof. An absent/empty store also returns normally without creating a
    file or schema; `SQLiteStore` remains the sole fresh-store initializer.
    Unknown, mixed, poisoned, or failed existing transitions raise write-free.

Only after independent B0 + B-schema + R2a/R2a.1 and the independently
reachable B1 items 11, 12, 13, 16, the item 15 GREEN control, and the item 17
writer matrix are reviewed may the minimal R2 behavior needed for item 13 be
implemented. Item 14 is frozen
only against the resulting real completion. The transition surface/behavior
microcycles remain separately gated as item 17 requires. Run the named
runtime/effect crash, parallel-delivery, and service-control tests from
`engine/` at every relevant stage.

The final R2 cutover is one coherent lifecycle-owner change set. Before its
production edit, mechanically move B0/B0.5 observations and recovery actions
to the v2 page API and the sole recovery driver, and freeze a read-only
`RecoveryDriver` constructor port `exclude_run_drive: Callable[[str], bool]`.
The service supplies a dynamic predicate over its ephemeral
`_initial_recovery_exclusion`; an excluded row is not an attempt, does not
consume the recovery limit, and does not stop the fixed-population scan.
Then remove `EffectDispatchWatch`, `list_dispatch_watches`,
`acknowledge_dispatch_watch`, `_recover_start_admissions`,
`_ack_start_if_observable`, and `EngineDriveService.acknowledge_start`
together. `RuntimeExecutionRecovery` uses the bounded v2 high-water/page read
only for composition discovery. Only
`RecoveryDriver._settle_terminal_watch -> reconcile_consumed ->
acknowledge_run_drive_watch` owns watch deletion. Compatibility aliases,
dual-read, and dual-ack phases are forbidden because they retain two reachable
lifecycle owners.

**R2 cutover correction after first GREEN diagnostic (2026-08-27).** The
initial production attempt exposed two fail-closed correctness gaps and one
test-placement overconstraint; these are plan defects, not reasons to restore
the retired lifecycle. Preserve the sweep high-water captured before the
migration page. Applying at most one bounded migration page and scanning the
ordinary watch population are independent: migration incompleteness must not
block already-durable v2 watches through the original high-water. After that
ordinary scan, process a second bounded cohort looked up by exactly the
successfully committed page's `inserted_public_run_ids` (at most 128), whether
or not that page completed the whole migration. Never derive this cohort from
a post-migration MAX or sequence interval: concurrent admissions remain next-
sweep work. After a crash following page commit, the next sweep sees its rows
through the ordinary high-water. The internal ledger port is
`list_run_drive_watches_by_public_run_ids`: it accepts only a sorted, unique,
non-empty tuple of 1..128 non-empty IDs and returns only matching durable DTOs
in `admission_seq` order; unknown and concurrent/unrequested IDs are absent.

`RecoveryDriver` remains the sole automatic discovery/replay/terminal-watch
owner, but generic protected Scope/Effect/Accept/Publish progression belongs
to the existing `EngineDriveService`. Inject the exact run-id-only private port
`drive_recovered_run: Callable[[str], bool]`, whose
boolean result means an engine-owned attempt was actually accepted; the
adapter must re-read authoritative catalog/native state and must not receive a
watch, binding, snapshot, grant, or migration fact. Worker parks, terminal
cleanup, busy work, and capacity deferral return false and consume no accepted
limit; concrete prepared/delivered coordinator progress returns true. The
runner-free Decision path remains the driver's narrow direct `reconcile_one`
path, and terminal cleanup remains driver-only. After any accepted direct or
delegated drive, the driver re-reads the native snapshot and, if it is now
terminal, performs its sole consumed-cleanup/watch-ack path in the same
attempt; watch deletion must not require a second explicit recovery. A
fairness oracle may assert
only durable/native outcome and protected-fact non-mutation, not whether the
retired service-private `_drive_engine_owned` placement was called.
Likewise, an idempotence test that captures its baseline only after writable
activation must treat correct same-activation recovery as carried GREEN:
baseline, first explicit recovery, and second explicit recovery are identical
and terminal. The neighboring crash/backfill tests, not that post-activation
baseline, prove the one required transition.
The recovered escalation oracle must assert durable native
`lockstep_outcome == "FAIL"`; `escalated` is its public status projection and
`ESCALATED` is not a member of the closed native outcome domain.
Final-cutover tests that seed a legacy catalog through current v2 command APIs
must explicitly delete the newly admitted v2 watches before asserting the
legacy no-watch precondition. Run-id-only generic delegation necessarily adds
one authoritative service-side snapshot after the driver's discovery snapshot;
and a replay that reaches accepted protected engine progress returns true on
that first attempt, then false once parked without new progress.

Composition discovery must page through the single captured v2 watch high-
water, scanning past any number of recipes with no runtime requirements while
collecting at most the requested number of composition-relevant bindings. It
must not restart at sequence zero and starve a later managed/pinned watch; it
adds no durable cursor and remains a read-only fixed-population scan.

Engine-drive loop continuation and recovery accepted-attempt accounting are
distinct closed action sets. Continuation remains the narrow monotonic set
`prepared`, `launch_claimed`, `sealed`, `delivered`, `awaiting_delivery`.
Recovery budget additionally counts provider/publication work reported as
`publication_claimed`, `publication_progress`, `running`,
`quiescence_pending`, or `indeterminate`. `busy`, `unchanged`, `no_effect`,
`manual_pending`, `acceptance_pending`, `authority_blocked`, and
`deadline_blocked` are non-attempt blockers/parks. Thus `limit=1` cannot
perform provider or publication work for an unbounded number of watched runs.

### Gate C — before any Task 12A production change

#### Gate C reachability correction (2026-08-27)

Independent contract and architecture review found that a single pre-production
freeze of all items below would be false TDD: items 5–15 require the real owner
journal, lock, replacement/no-clobber, and fsync boundaries, while guessed
private hooks would freeze an accidental implementation and missing-module
imports would hide the intended behavioral oracles. Preserve the complete Gate
C contract, but reach it in these reviewed microcycles:

1. Freeze public CLI/MCP/authoring closure behavior plus the exact plan-named
   immutable `SourceIdentity`, `DestinationImage`, `ProjectCompilationBundle`
   and `AuthoringPublisher.publish(bundle)` / `recover(project)` surface. Record
   parse/semantic/missing/cycle write-freedom as carried GREEN controls. No
   authoring production edits.
2. Add only the policy-free immutable DTO/publisher surface and a fail-closed
   ordinary-compile planner boundary. Then freeze one reachable construction RED
   for an ordinary leaf plan: it returns a non-empty immutable bundle with the
   exact source identity, dependency edge, before-absence set, and the exact
   reachable recipe/manifest/source-map after-image map while performing no
   write. Generated specialized-child outputs are explicitly deferred to the
   next direct-child planner RED; overall whole-DAG Gate C still requires every
   generated output.
   Do not choose the template planner signature, journal schema internals,
   replacement loop, fault hooks, or publication policy in these microcycles.
3. Against those reachable natural boundaries, freeze the remaining bundle,
   filesystem, journal, fault, recovery, and concurrency REDs below. Tests wrap
   real filesystem primitive/collaborator calls and count ordinals locally;
   production must not add a test-only `_after_replacement` lifecycle hook.
4. Only after independent review of the complete reachable Gate C matrix may
   bundle/publisher behavior proceed RED→GREEN.

**First Gate C microcycle ready 2026-08-27:** the tests-only range freezes the
two independent named surfaces, `AuthoringPublisher(state_dir)`, both packaged
templates through CLI and MCP, a three-level public CLI compile, public
check/diff remediation, exact recipe/dependency/source-map/generated closure
bytes, and four carried planning write-freedom controls. Focused evidence is
exactly `8 failed, 4 passed`; the failures are the two absent surfaces plus six
whole-DAG behavior oracles. Existing template/recipe authoring remains `58
passed`; compileall and diff check are clean. Independent contract and
architecture/threat reviews PASS with C/I/M zero. No production file changed.

**Policy-free surface GREEN completed 2026-08-28:** real frozen/slotted domain
values separate stable path identities from regular-file leaf identities and
bind child-first sources, dependency edges, and exact before/after images
without filesystem I/O or publication policy. `AuthoredRecipe` has one inward
owner and preserves its public re-export identity. The explicit-state publisher
constructor is cwd-independent; planner, publish, and recover stop fail-closed
before any effect. Evidence: surface `2 passed`; Gate C now exactly `6 failed,
6 passed` with only the six whole-DAG behavior REDs remaining; existing
template/recipe authoring `58 passed`; architecture `22 passed` with the one
existing reviewed warning; compileall and diff check clean. Contract/threat,
feasibility, and architecture/SRP reviews PASS with C/I/M zero. The next
microcycle is the now-reachable exact leaf construction RED.

Create `engine/tests/test_authoring_bundle.py` and
`engine/tests/test_authoring_publisher.py`; extend template and recipe CLI tests.

1. `test_parent_compile_rebuilds_changed_direct_child[template,adapter]`: one
   public parent compile via CLI and MCP publishes the exact full plan and every
   role canonical-matches.
2. `test_parent_compile_rebuilds_transitive_grandchild`: one command publishes a
   three-level child-first DAG with no stale intermediate output.
3. `test_parent_check_and_diff_cover_child_closure`: child-only drift is visible;
   one parent compile makes both clean.
4. `test_compile_planning_failure_is_write_free[...]`: child parse/semantic/
   compile failure, missing child, cycle, cross-root collision, aggregate limit,
   symlink/non-regular source or destination, and ancestor swap leave exact
   bytes/modes and no journal.
5. `test_source_change_after_plan_is_not_overwritten_or_published`: change a
   child before first replacement; source remains and outputs remain all-old.
6. `test_source_change_mid_publish_rolls_outputs_back`: edit a source after each
   replacement ordinal; recovery preserves it and restores exact output
   bytes/modes/absence.
7. `test_publish_fault_restores_existing_bundle[ordinal]`: cover journal
   durability, each file replace/fsync, each parent-directory fsync, and journal
   cleanup; raised failure or next-command recovery is all-old.
8. `test_recovery_refuses_foreign_postcrash_mutation`: neither-before-nor-after
   destination is preserved with trusted evidence and no partial cleanup.
9. `test_next_command_recovers_or_fails_closed[compile|install|check|diff|canonical]`:
   no command observes or blesses a mixed bundle.
10. `test_template_and_compile_share_publisher_with_distinct_preconditions`:
    same lock/journal/recovery; install requires all absent, compile replaces
    generated write set only.
11. `test_authoring_recovery_restores_mode_and_absence`: executable/read-only
    modes and new-path absence are exact; only newly created paths are removed.
12. `test_concurrent_authoring_transactions_never_mix`: serialization yields
    exactly one complete plan.
13. `test_owner_journal_is_project_bound_and_aggregate_bounded`: project-local
    pointer/edits cannot redirect trusted recovery; 256/4 MiB ceilings fail
    before journal creation.
14. `test_foreign_destination_change_before_own_replacement[ordinal,edit|create]`:
    immediately before every destination's mutation, edit an existing later
    leaf or create an absent later template leaf. The per-destination check or
    no-clobber primitive preserves that foreign path, rolls back only previously
    transaction-written destinations to exact old bytes/modes/absence, and
    retains trusted evidence until rollback is durable.
15. `test_final_read_set_validation_is_commit_point`: mutate a source after the
    last replacement but before final validation; publication does not commit,
    preserves the source edit, restores transaction-owned outputs, and does not
    remove the journal before rollback durability.

Run from `engine/`:
`uv run pytest tests/test_authoring_bundle.py tests/test_authoring_publisher.py tests/test_templates.py tests/test_recipe_cli.py tests/test_cli.py tests/test_server.py -q`.

**Template cutover prerequisite inserted 2026-08-28:** invariant 10 forbids
modeling temporary/package template inputs as ordinary project
`SourceIdentity`. Before the template transaction RED, freeze and GREEN the
minimal bundle role contract: non-empty child-first `dependency_edges` owns the
role inventory; `sources` is either the exact complete ordinary role set or
empty for a destination-only template bundle, never partial. Then freeze
empty-read-set journal recovery and the CLI-only template publisher/crash
cutover. Legacy `.lockstep/.template-install.json` is inert untrusted project
data: never parse/delete/migrate/recover from it; preserve it while the normal
complete all-absent destination policy decides whether installation can
proceed. Reassess Tasks 13+ only after all Task 12 work, as already required.

### Gate P — before any Task 12C installed-artifact change

Complete the blocking Task 12A.5 complexity, proportionality, and product-goal
review from the master plan. This gate is read-only with respect to production
behavior: it measures and adjudicates the completed architecture before more
surface is added.

1. Measure Lockstep production and tests separately and compare reproducibly
   with the exact installed YAMLGraph and relevant LangGraph distributions.
2. Map each substantial subsystem to a current product requirement, reachable
   threat-model path, and unique responsibility; presumptively remove anything
   without all three.
3. Identify custom mechanisms duplicating YAMLGraph, LangGraph, SQLite, or OS
   primitives and quantify the cost and lost guarantees of replacing them.
4. Compare complete `retain`, `simplify`, and `replace/redesign` alternatives;
   reject arguments based on sunk cost or green tests alone.
5. Establish a numeric complexity budget for the remaining work and obtain
   independent scope/proportionality, architecture, and threat-model reviews.
6. Obtain explicit user approval of `keep`, `simplify`, `redesign/re-scope`, or
   `stop`. If remediation is selected, approve and complete its own range before
   Gate D. No Task 12C production/test cutover work may begin earlier.

### Gate D — before any Task 12C installed-artifact change

Extend `engine/tests/test_plugin_packaging.py` and
`engine/tests/test_task12_plugin_identity.py`; create
`engine/tests/test_installed_contract.py`.

1. `test_packaged_distribution_has_no_retired_contract`: build the wheel and
   scan its file list/content plus active README, `docs/DESIGN.md`, both skills,
   examples, manifests, and runtime source for `_subcall`, `lockstep.subcalls`,
   `_subcall_wrapper.py`, `Subcalls (v2)`, fractal-subcall prose,
   `runners.yaml`, `LOCKSTEP_RUNNER`, `RunnerSpec`, `load_runners`, and retired
   runner-config prose. Only historical changelog/superseded spec are excluded.
2. `test_legacy_runner_module_not_packaged_or_importable`: no legacy module,
   symbols, build argv, or production importer.
3. `test_native_skill_examples_compile_and_match_registered_tools`: every
   active code/command example parses, validates, compiles, and names real
   CLI/MCP surfaces.
4. `test_packaged_docs_smoke_matches_public_composition`: follow documented
   provisioning for credentialed codex and credential-free pinned, install both
   templates, preflight, start, observe, and explicitly recover; configuration
   alone grants neither selector OS execution.
5. `test_estimate_schema_uses_peak_parallel_child_calls`: public outputs contain
   the new field and never the retired field.

Run from `engine/`:
`uv build && uv run pytest tests/test_installed_contract.py tests/test_plugin_packaging.py tests/test_task12_plugin_identity.py tests/workflow/test_estimate.py tests/test_recipe_cli.py -q`.

## Exact order, review gates, and stop rules

1. **12R0 ownership/A0/A-schema/B0/B-schema RED freeze:** commit this ownership
   contract plus independently failing A0, A-schema, B0, and B-schema only.
   Independently review exact baseline reasons and absence of production edits.
   R1a and R2a stop until approved.
2. **12R1a inert projection/schema:** implement only the reviewed policy-free
   R1a surface through explicit one-reachable-oracle RED→minimal-GREEN
   microcycles for A4 projection isolation, field shape/closedness, parser and
   JSON validation, inventory/listing, digest vectors, and snapshot
   normalization. Make
   A-schema/R1a controls GREEN while every B0 parameter still
   completes setup and fails at its recorded final fact/trace oracle. It is an
   unreleased intermediate; templates are not release-ready.
3. **12R1b-P provisioning:** add/review only genuine persistence/generation/
   concurrency REDs against R1a (R1a validation controls remain GREEN), then
   implement and make that group GREEN. Re-run B0 preservation.
4. **12R1b-A0 static admission:** add/review only the fully granted real
   `codex` and `pinned` positive REDs while A8/A9-A12 remain exact GREEN safety
   controls. Implement immutable snapshot capture, binding, exact full-index
   grant preflight, and an immutable admission decision; make the two positive
   REDs GREEN and preserve B0. No currentness, resolve, commitment, or spawn.
5. **12R1b-A1 first-write currentness:** only after A0 GREEN, add/review A16
   with a deterministic barrier after the original real preflight and supported
   provisioning drift. Implement the shared owner-lock currentness guard through
   the first-write critical section, make A16 GREEN, rerun A8/A9-A12 and B0.
6. **12R1b-E0 gate transition:** carry the packaged-template no-spawn/no-grant
   cases as GREEN controls; add/review only A5 using the separate real protected
   managed closure; then land/review the tests-only A0/A1 gate transition. The
   evolved pre-production gate is `218 passed, 1` intentional A5 RED after the
   final obsolete cache-only shutdown control is retired.
7. **12R1b-E0 composition/commitment GREEN:** only after the gate-transition
   PASS, implement/review A5 GREEN. During architecture review, freeze the
   independently reachable active-command installation oracle before fixing
   it. The canonical E0 Gate A includes `test_service_controls.py` and is
   exactly `243` collected; require `243 passed` before E0 approval.
8. **12R1b-E1 selector lifecycle:** only on reviewed E0 GREEN, add/review A6 as
   an independently reachable GREEN lifecycle control and A7 as the sole honest
   RED at the closed `verify → pinned` semantic-kind gate, then implement/review
   A7 GREEN without kind rewriting or generic-kind acceptance.
9. **12R1b-E2 restart:** add/review A15 only after the public lifecycle is GREEN,
   then implement/review restart reconstruction GREEN.
10. **12R1b-E3 after-resolve drift:** add/review A16 only after real resolution
   and durable preparation are reachable. If the exact future oracle is already
   GREEN through the original production commitment guard, land/review it as a
   tests-only carried control with no artificial RED or production change. Any
   later change making packaged templates authority-bearing belongs to Task 12C
   after its own RED freeze. R1 completes only when all A1-A17 parameters and
   combined controls are GREEN, B0 remains its reviewed RED, and the complete R1
   range has independent architecture/security review.

   **Completed 2026-08-27.** Combined review first found one Important
   workspace service-locator residue rather than accepting the earlier thin
   facade. Reviewed RED `2785cd6` and GREEN `91ffe90` establish one frozen
   data-only context plus explicit typed record/attestation collaborators with
   byte-identical durable records and identical lock/rename/fsync ordering.
   Follow-up ownership cleanups `38fe8dc` and `31cca13` remove lazy provisioning
   and ingress re-exports and all range whitespace findings. Final architecture,
   threat-model/security, and behavior reviews are PASS with C/I/M zero. Final
   HEAD `31cca13`: Gate A `258 passed`; non-R2 `1207 passed, 1 skipped`, one
   unchanged reviewed warning; frozen R2 exact `29 failed`; compileall, rebuilt
   graph, status, working diff, and `db1204f` range diff clean.

11. **B0.4 native child→parent lineage prerequisite:** freeze and independently
   review the exact two-checkpoint port-surface RED, make only that neutral
   wiring GREEN, then freeze/review the real live+restart cross-namespace RED
   and complete adversarial fail-closed controls before implementing the bounded
   native runtime/adapter bridge. Independently review each microcycle and the
   complete range. R2 and B0.5 watch assertions remain stopped.

   **Completed 2026-08-27:** `7078588`, `3d1c282`, `674fe69`, `bc83324`;
   independent architecture/security PASS, C/I/M zero. B0.5 is unblocked.
12. **B0.5 acceptance-lifetime RED freeze:** before any R2 policy behavior,
   extract/review the real child-artifact/producer/consent/pending-acceptance
   fixture and its early-old-watch-deletion RED. Tests and frozen design only;
   no `FakeRuntime` or synthetic native snapshot. R2a stops until approved.

   **Completed 2026-08-27:** commit `012fd10`; shared real compiled managed-
   child fixture, native restart control GREEN, and sole B0.5 final watch-
   lifetime RED. Independent threat-model and architecture reviews PASS with
   C/I/M zero. R2a is unblocked.
13. **12R2a durable-protocol skeleton:** implement and independently review only
   the policy-free B-schema surface and private natural boundaries. Make
   B-schema plus R2a protocol invariants GREEN; run B0 preservation again and
   keep it intentionally RED. No classification, recovery/lifetime/fairness/
   backfill policy, or pump behavior may land. Then commit and independently
   review B1 missing-policy REDs only; R2 behavior stops until B0 is preserved
   RED, B-schema/R2a protocol checks are GREEN, and B1 fails at its reviewed
   missing-policy oracles.

   **R2a completed 2026-08-27:** the DTO/DDL, epoch fence, bounded ledger
   reads, atomic acknowledgement, durable classified-page protocol, and private
   inert `RecoveryDriver` landed as independently reviewed RED→GREEN
   microcycles through `3e226a8`. Final contract/security and architecture/SRP
   reviews both report C/I/M zero and `R2a COMPLETE`. The focused gate is `44
   passed`; B0/B0.5 preservation remains exactly `7 passed, 17 failed` at the
   frozen missing-policy oracles; effective non-R2 is `1250 passed, 1 skipped`
   after the two sandbox-only nested-uv tests pass outside the sandbox.
   `compileall`, `git diff --check`, and worktree status are clean. No
   classification, page iteration, recovery/lifetime/fairness/backfill policy,
   pump activation, or public surface entered R2a. The next step is the B1
   tests/design-only RED freeze; R2 production remains stopped pending its
   independent review.

   **R2a.1 reachability correction:** before B1 policy REDs, add and independently
   review only the exact inert private sweep surface plus neutral automatic and
   explicit command routing specified above. Then freeze/review B1 items 11,
   12, 13, 16, the item 15 GREEN control, and the epoch-1 writer matrix. Item 14
   and the two-process transition test remain deliberately deferred to their
   dependency points.

   **R2a.1 completed 2026-08-27:** independently reviewed RED→GREEN commits
   `95c001a..374266d` add the sole exact inert sweep boundary and neutral
   automatic/explicit routing. Final contract/security and architecture/SRP
   verdicts are `R2a.1 COMPLETE`, C/I/M zero. The focused R2a/service gate is
   `76 passed`; frozen B0/B0.5 remains the same `17 failed, 7 passed`; effective
   non-R2 is `1253 passed, 1 skipped` after the same two sandbox-only nested-uv
   tests pass outside the sandbox. `compileall`, `git diff --check`, and status
   are clean. B1 item 11 is next; no classification, migration iteration,
   lifecycle, fairness, cleanup, or other R2 policy has landed.
14. **12R2 driving/migration:** implement watch lifetime, fairness, cleanup, and
    restart-complete v2 migration in reviewed microcycles. First make item 13
    GREEN, then freeze item 14 against that real completion. Before the
    two-process item 17 transition RED, add/review its fail-closed private
    surface; implement the transition while unwired, then freeze and make GREEN
    a separate command pre-open reachability microcycle. Make all B0/B1 behavior
    green; run the complete existing effect crash/parallel and service-control
    matrix. Independently review the R1+R2 combined runtime range.

    **12R2 completed 2026-08-27:** the coherent final cutover physically removes
    the legacy lifecycle surfaces, makes `RecoveryDriver` the sole automatic and
    explicit replay/watch-ack owner, preserves a fixed sweep high-water plus the
    exact committed migration cohort, and delegates generic protected progress
    through the run-id-only accepted-attempt port. Runtime composition discovery
    pages past irrelevant watches without unbounded materialization. Atomic native
    binding ownership, owned reservation rollback, pump handoff/release, and the
    canonical activation → admission lock order close the reviewed concurrency
    races without a second lifecycle manager. Final evidence: expanded cutover
    gate `131 passed`; complete runtime `765 passed`; compileall and diff check
    clean; independent contract/threat, feasibility/concurrency, and
    architecture/SRP reviews all PASS with C/I/M zero. The next milestone is the
    combined R1+R2 review; Tasks 12A/12C and Task 13+ remain untouched.

    **Combined R1+R2 review completed 2026-08-27:** cross-range concurrency
    analysis found and reproduced one composed deadlock between static admission
    (`snapshot.lock → admission`) and the pump authority path
    (`admission → snapshot.lock`). `_WritableCoreActivation` now receives the
    service's sole admission `RLock` and orders the transaction as admission →
    current owner snapshot → nonblocking activation → durable persist. On
    activation contention it releases both outer locks before blocking and
    retries currentness from scratch. Exact-order and real two-thread regressions
    are GREEN. Final full-project evidence: `1360 passed, 1 skipped`, one existing
    reviewed architecture warning; compileall/diff check clean; combined
    contract/threat, feasibility/concurrency, and architecture/SRP reviews all
    PASS with C/I/M zero. Task 12A is next; later-task reassessment remains at the
    milestone after Task 12 completes.
15. **12A RED freeze:** in a separate range, follow the Gate C reachability
   correction above: commit/review the exact public + immutable-surface RED with
   no authoring production edits, land/review the policy-free skeleton, then
   commit/review the complete behaviorally reachable C RED matrix. No publisher
   policy work starts before that approval.
16. **12A bundle/publisher:** implement only the A files/contracts; make C green;
   run template, recipe CLI/MCP, ingress, compiler, and filesystem fault tests;
   independently review.

   **Combined 12A review correction (2026-08-28):** the first combined range
   review of `1a75172..09ab0f4` failed despite a green full suite because not
   every frozen Gate C oracle had been implemented. Complete these reviewed
   RED→GREEN microcycles before Task 12A may close, in dependency order:

   1. route canonical/start ingress through recovery plus one serialized
      immutable observation, completing Gate C item 9;
   2. cut `write_compilation` and public CLI/MCP `recipe init` over to explicit
      owner state and the shared authoring transaction; no direct project writer
      remains on that public path;
   3. bind check/diff/canonical to one captured whole-DAG plan/result rather than
      a second recursive filesystem pass;
   4. freeze separately the complete item 4 planning/no-journal matrix and item
      13 project-binding/256-record/4-MiB owner-state bounds, carrying cases
      already GREEN without artificial failures;
   5. complete item 7 durability cuts and item 14 per-destination foreign
      edit/create oracles on shared real fault infrastructure;
   6. freeze and satisfy item 8 durable recovery of a neither-before-nor-after
      foreign mutation;
   7. only after every public writer and observer uses the common protocol,
      freeze and satisfy item 12 writer-vs-writer serialization, then rerun the
      combined Gate C and independent threat, reliability, and architecture
      reviews.

   **Microcycle 3 completed 2026-08-28:** tests-only RED `9b8a877` and
   production GREEN `a0cd158` bind check, diff, canonical matching, and public
   generated-recipe preflight to one captured whole-DAG plan. The immutable
   result projector proves every before image against every planned after
   image, derives the candidate and proof from the same captured bytes, and
   performs no second recursive project-filesystem ingress. The classification
   leaf uses bounded no-follow capture plus the recipe-owned strict YAML
   limits; recovery tests observe unlocked optimistic classification, locked
   recapture, and zero ingress at an unready boundary. Final evidence: affected
   `368 passed`; architecture `21 passed` with one unchanged reviewed warning;
   full engine `1473 passed, 1 skipped`; compileall and diff checks clean.
   Independent architecture, threat, and reliability reviews PASS with
   Critical/Important/Minor zero. Microcycle 4 is next; Task 12C remains
   blocked.

   **Microcycle 4 completed 2026-08-28:** tests-only contract commit `e5bc69f`
   freezes the complete Gate C item 4 planning/no-journal matrix and item 13
   project-binding plus independent resource bounds. Every reachable contract
   was already GREEN, so no artificial RED or production change was introduced.
   Public planning now proves parse, semantic, lowering, missing, cycle,
   source/destination type, collision, ancestor-swap, 257-record, and aggregate
   failures leave the exact project unchanged and create no trusted journal.
   Direct `AuthoringPublisher.publish(bundle)` independently revalidates 257
   sources, 257 paired destinations, and read/before/after aggregates before
   owner namespace creation. Recovery ignores project-local redirect data and
   remains bound to the exact resolved project identity. Evidence: focused and
   architecture `43 passed`; original bundle/publisher `57 passed`; diff check
   clean. Independent architecture, threat, and reliability reviews PASS with
   Critical/Important/Minor zero. Ruling: per-file wording emitted when the
   remaining aggregate allowance is smaller than the next individually valid
   leaf is not a product defect because the required bounded rejection,
   write-freedom, and no-journal guarantees already hold; changing production
   only for diagnostic taxonomy would be out of scope. Microcycle 5 is next;
   Task 12C remains blocked.

   **Microcycle 5 completed 2026-08-28:** tests-only commits `10203a8` and
   `cadc612` freeze Gate C item 7 as one exact three-destination durability
   trace plus 24 crash cuts, and item 14 as edit/create races at every
   destination together with ordinary pre-link and post-link `EIO` controls.
   The probes bind live journal temp descriptors to the real owner journal
   directory, bind each rollback mutation to its file/parent durability
   barrier, compare complete project/owner namespaces, and preserve exact
   foreign inode identity. The matrix exposed one reachable correctness bug:
   `EEXIST` from the real no-clobber link proves that the staged inode was not
   published, but the live rollback still treated that foreign destination as
   ambiguously owned and retained stages/journal. Production GREEN `cb52a57`
   revokes only that just-enrolled ownership record on `FileExistsError`;
   generic `OSError` and post-link crash paths retain ownership evidence and
   roll back or recover conservatively. Evidence: new matrix `37 passed`;
   adjacent authoring/recovery `66 passed`; architecture `21 passed` with the
   unchanged reviewed warning; compileall and diff check clean. Independent
   architecture, threat, and reliability reviews PASS with
   Critical/Important/Minor zero. Microcycle 6, Gate C item 8 durable recovery
   of a neither-before-nor-after foreign mutation, is next; Task 12C remains
   blocked.

   **Microcycle 6 completed 2026-08-28:** tests-only contract `6a27dec`
   freezes Gate C item 8 without production changes. The proportional matrix
   contains nine justified equivalence cells rather than the optional
   24-cell Cartesian expansion: six uncommitted cells cover both existing
   restore and absent remove mechanics, every crash and foreign-target
   ordinal, and all target-before/equal/after-crash relations; three committed
   cells cover every eager after-image observation position in the distinct
   committed recovery class. Every case reaches a real destination mutation
   plus parent fsync (or the real durable committed generation), parses the
   exact journal phase/progress under its existing lock, durably installs a
   third foreign inode, and requires two fresh recoveries to fail with zero
   mutations while preserving the complete project, owner, journal, stage,
   and foreign-file images. Evidence: new matrix `9 passed`; adjacent recovery,
   crash, and architecture controls `91 passed`; compileall and diff check
   clean. Independent architecture, threat, and reliability reviews PASS with
   Critical/Important/Minor zero. Microcycle 7, Gate C item 12
   writer-vs-writer serialization, is next; Task 12C remains blocked.

   **Microcycle 7 completed 2026-08-29:** tests-only contract `f206016`
   freezes Gate C item 12 as five proportional cooperating-writer cells: two
   overlapping replacement writers, two distinguishable overlapping template
   installs, disjoint replacement/template publication, queued recovery after
   a durable writer crash, and the recovered/planned publish gap. Every
   behavioral thread enters through the public compilation or template writer,
   contends on the exact project-bound persistent lock inode, and is joined
   within a bounded cleanup path. Exact mutation attribution proves losers are
   read-only, disjoint destination writes occur only inside the publish lock,
   and crash evidence is exactly one new destination plus the remaining owned
   stages and active journal. Full project/owner images, bytes, modes, inode
   identity, typed failures, and repeated mutation-free recovery are asserted.
   The ordinary module is `5 passed`; prepatching `fcntl.flock` to a no-op makes
   all five cells fail at the serialization oracle; adjacent authoring controls
   are `68 passed`; architecture controls retain only the previously reviewed
   warning. Three final independent reviews PASS with
   Critical/Important/Minor zero. The corrected seven-microcycle sequence is
   complete. Next: rerun the combined Task 12A range review; Task 12C remains
   blocked.

   **Second combined 12A review correction (2026-08-29):** the repeated range
   review of `1a75172..f206016` passed the bounded threat-model review with
   Critical/Important/Minor zero, but reliability and architecture reviews did
   not pass. The full engine suite also exposed a deterministic pre-existing
   native-app lifecycle defect: each parked worker retains two SQLite file
   descriptors until service shutdown, so 120 retained apps consume 248 file
   descriptors at the ordinary macOS soft limit of 256. Task 12A preflight is
   merely the first `EMFILE` crash site; it is not the allocator. Complete the
   following reviewed RED→GREEN corrections before Task 12A may close:

   8. freeze a reduced-`RLIMIT_NOFILE` 128-worker regression that proves file
      descriptors remain bounded and a later worker can be rebound and driven;
      protect failure cleanup with `try/finally`, then release parked native
      apps at the request boundary and use the existing catalog-backed lazy
      bind path;
   9. extend the real durability crash matrix through
      `committed.temp_fsync`, `committed.replace`, and
      `committed.parent_fsync`, with explicit all-old/all-new recovery outcomes,
      durable evidence retirement, and a write-free second recovery;
   10. remove the remaining public second whole-DAG traversal: recursive
       `compile_project_source` and `estimate_recipe` must project the one
       authoritative captured plan used by `write_compilation`, canonical,
       check, and diff rather than independently walking the project;
   11. decompose the Task 12A trusted-computing-base hotspots identified by the
       deterministic complexity review, extend the architecture guard to the
       new Task 12A symbols, extract the duplicated bounded no-follow descriptor
       observation primitive without merging caller policy, and split the
       mixed-responsibility authoring bundle and publisher test modules. Do not
       split the cohesive recovery parser merely to satisfy a line threshold;
   12. rerun focused and adjacent suites, compileall, diff checks, the complete
       engine suite, and the combined threat, reliability, and architecture
       reviews. Task 12A closes only on three independent
       Critical/Important/Minor-zero results.

   Stop on a wrong-reason RED, a test-only lifecycle seam, an ambient owner-state
   lookup inside the direct authoring API, a second lock/driver/planner, or any
   change to public output schemas. Task 12C remains blocked until the repeated
   combined review is PASS with Critical/Important/Minor zero.

   **Microcycle 8 completed (2026-08-29):** tests-only RED `c403909` and GREEN
   `304f88a` bound parked native-app lifetime to an active request/effect owner,
   retain durable pre-/post-handoff recovery, and lazily rebind through the one
   catalog-backed lifecycle scope. The reduced `RLIMIT_NOFILE=64` matrix parks
   and resumes 128 runs without descriptor growth; deterministic controls cover
   pre-handoff cleanup, post-handoff ownership, owner/borrower serialization,
   cancelled explicit wakeups, timed empty recovery, same-process watch
   adoption, cold restart, foreign/private/path-shaped run indistinguishability,
   write-free session rejection, and stale-session rejection before native bind.
   Final evidence: affected `126 passed`; dependency wheel/build `2 passed`;
   complete engine `1556 passed, 1 skipped`, with only the previously reviewed
   `_reconcile_publication` cohesion warning; compileall and diff checks clean.
   Independent reliability and threat/architecture reviews PASS with
   Critical/Important/Minor zero. No external publication occurred. Next:
   microcycle 9, complete committed-marker crash cuts and durable evidence
   retirement.

   **Microcycle 9 completed (2026-08-29):** tests-only REDs `df48b27` and
   `a70e0c8`, followed by GREEN `06184cd`, extend the real syscall crash matrix
   through `committed.temp_fsync`, `committed.replace`, and
   `committed.parent_fsync`. Recovery is explicitly all-old before commit
   replacement and all-new after replacement. The process-death harness blocks
   post-cut Python cleanup, so every fsynced journal temporary remains real
   crash evidence. Recovery admits at most 64 reserved-name temporaries, sorts
   them, verifies the entire owner-only regular-file set before any mutation,
   unlinks the proven set under the existing project lock, and fsyncs the exact
   journal directory. Both valid/insecure enumeration orders fail closed with
   zero mutations and byte/inode-identical namespaces. Exact evidence
   retirement and a write-free second recovery are frozen. Final evidence:
   focused `30 passed`; adjacent authoring `191 passed`; complete engine `1561
   passed, 1 skipped`, with only the previously reviewed
   `_reconcile_publication` cohesion warning; compileall and diff checks clean.
   Independent reliability and threat/architecture reviews PASS with
   Critical/Important/Minor zero. The first full-suite attempt used a relocated
   stale venv activation path and sandbox-denied uv cache; the authoritative
   rerun used `uv run --project .` from the persistent worktree. No external
   publication occurred. Next: microcycle 10, remove the remaining public
   second whole-DAG traversal.

   **Microcycle 10 completed (2026-08-29):** tests-only RED `574667c` and
   GREEN `e073a49` remove the recursive public planner. The one existing
   `_plan_project_compilation` capture now retains the same-pass root
   `ValidatedWorkflow`, `ResolvedCatalog`, and `CompilationResult` alongside
   its immutable bundle. `compile_project_source` returns that complete tuple
   and workflow `estimate_recipe` derives its structural result from the same
   plan; neither reopens the child DAG. Causal tests mutate a child compilation
   input or change the root from one child call to two immediately after plan
   capture, prove the alternate estimate differs, and require the full public
   projections to remain pre-mutation values with exactly one planner call.
   Public result schemas and ordinary source argument remain unchanged; the
   removed `_cache`/`_active` arguments were private recursion machinery.
   Final evidence: causal `2 passed`; related authoring `64 passed`; complete
   authoring plus architecture `213 passed`; complete engine `1563 passed, 1
   skipped`, with only the previously reviewed `_reconcile_publication`
   cohesion warning; compileall and diff checks clean. Independent reliability
   and threat/architecture reviews PASS with Critical/Important/Minor zero. No
   external publication occurred. Next: microcycle 11 TCB decomposition,
   descriptor-observation extraction, and mixed test-module split.

   **Microcycle 11 completed (2026-08-29):** tests-only RED `7cfa323` and
   GREEN `335ba2c` add the five independently SRP-adjudicated Task 12A TCB
   roots to the deterministic architecture guard and decompose each below its
   cyclomatic/cognitive/nesting/fan-out limits. One policy-free descriptor
   observer now owns `fstat -> bounded read -> fstat -> close`; planning,
   identity, recovery, and trusted-journal callers retain their distinct open,
   path binding, absence, limit, UID/mode, digest, ownership, and error
   policies. The former bundle and publisher test modules are split by
   contract, capture, closure command, publication, crash recovery, read
   recovery, stage recovery, namespace, fault, and scenario responsibility;
   all 30 original test functions remain exactly once. The cohesive
   `_RecoveryParser.parse`, `DestinationImage.__post_init__`, and
   `AuthoringTransaction._cleanup_stages` are not divided merely for a line or
   isolated metric threshold. Final evidence: focused plus architecture `82
   passed`; complete engine `1568 passed, 1 skipped`, with only the previously
   reviewed `_reconcile_publication` cohesion warning; compileall and diff
   checks clean. Independent reliability and threat/architecture reviews PASS
   with Critical/Important/Minor zero. No external publication occurred. Next:
   microcycle 12 combined Task 12A verification and three independent final
   range reviews.

   **Microcycle 12 and Task 12A completed (2026-08-29):** the authoritative
   final engine suite is `1568 passed, 1 skipped`, with only the unchanged
   reviewed `_reconcile_publication` cohesion warning. Compileall, worktree
   status, and `git diff --check 1a75172..932e4b0` are clean. Independent
   threat-model, behavior/reliability, and architecture/SRP reviews of the full
   range each PASS with Critical/Important/Minor zero and no evidence gaps.
   The reviews separately revalidated every Gate C crash/concurrency/foreign
   recovery vector, bounded lifecycle and descriptor ownership, one-plan and
   one-lock architecture, caller-specific filesystem policy, public schema
   stability, and the absence of moved structural complexity. Task 12A closes;
   Gate P / Task 12A.5 is now the blocking next milestone and Task 12C remains
   unstarted pending its explicit user-approved decision.
17. **12A.5 proportionality decision:** execute Gate P after the complete Task
   12A range review. Measure production and tests separately, attribute cost to
   requirements and reachable threats, compare retain/simplify/replace
   alternatives, set the remaining complexity budget, and obtain explicit user
   approval. Complete any selected remediation before continuing.

   **Analysis/review completed (2026-08-29):** the decision-grade audit is
   `.superpowers/reviews/2026-08-29-task-12a5-proportionality.md` at `97893e3`.
   Product/proportionality, architecture/SRP, and threat-model reviews all PASS
   C/I/M zero. Only explicit user selection remains. The reviewed choices are
   `keep`, `simplify-with-write` (recommended),
   `simplify-owner-applied-patch`, `redesign/re-scope`, or `stop`. No selected
   remediation plan exists yet, and Task 12C remains blocked.
18. **12C RED freeze and cutover:** only after R1+R2 and A public APIs are final
   and Gate P is approved,
   commit D REDs/review, then update active artifacts, remove legacy code, and
   make D green.
19. **Release gate:** from `engine/`, run `uv run pytest -q`, `uv run python -m
   compileall -q src tests`, `uv build`, install the wheel into a clean temporary
   environment, repeat D's black-box smoke, and verify `git status --short` plus
   the intended diff. Obtain independent architecture and reliability reviews
   over the separate runtime, authoring, and installed-contract ranges.

Stop immediately and return to the owning unit's RED/design review if any of
these occurs: a RED is absent, unexpectedly passes on its stated baseline,
fails for the wrong reason, or
uses a source-string, tautological, early-common, or malformed bare-catalog
oracle; production changes precede that unit's reviewed RED commit; projection reaches
command dependencies; preflight writes before rejection; a selector outside
`codex`/`pinned` is introduced; configuration implies authority; acceptance
consent is demanded at admission; provisioning merges recipe-local grants,
retains an omitted grant, asks callers for generation/binding-bound requirement
digests, cannot map predecessors by stable selection key, or changes generations
for idempotent input; a
commitment cannot reconstruct one exact requirement or survives owner drift;
Decision gains a ledger row; watch discovery lacks an immutable sequence
high-water; migration data is used after schema completion or gains workflow/
scheduler semantics; a legacy writer can commit after the v2 epoch fence; a
capacity-deferred row prevents later fixed-population work or loses next-sweep
eligibility; a fresh driver can starve later work; authoring source enters the ordinary write
set; any destination is mutated without its immediate identity/absence and
ancestor check; final read-set validation is not the commit point; recovery
deletes a foreign edit; Task 10 publication owns authoring; active guidance
   still names a retired API; any focused or full regression outside the sole
   temporary exception fails; or the worktree contains unexplained changes. The
   exception is only a named, independently reviewed A0/A-schema/R1b or
   B0/B-schema/B1 RED at its recorded reason in its specified stage; every
   unrelated failure, setup/signature failure, or changed named oracle remains
   a stop.

No Task 13, version/release, publication, or release-ready template claim is
allowed until A/B/C/P/D and the combined release gate are green and reviewed.

## Self-check

- B1 resolved: composition is closed over both released selectors; managed and
  pinned use distinct credentialed/credential-free bindings; the stable
  `grant_selection_key` excludes binding and every generation while the exact
  current `requirement_digest` adds binding/config identity. Complete-snapshot
  replacement maps selection keys to current requirements, deterministically
  reissues across config changes without caller prediction, and preserves exact
  descriptor-to-requirement reconstruction and preflight-to-commit drift/
  revocation behavior. The bounded parsed JSON provisioning schema, external
  owner-only TMPDIR, product-visible requirement listing, static-inventory then
  private-bound-view bootstrap, exact canonical uses, complete-tuple-only
  coalescing, admission-currentness critical section, and shared owner lock are
  frozen. Final owner drift preserves prior immutable attempt audit while
  preventing spawn/observation/delivery/continuation. Static runtime authority,
  dynamic coordinate-bound `EffectGrant`, and bearer-consent boundaries remain
  separate.
- B2 resolved: nullable-input watch v2 and monotonic, bounded, restart-complete
  migration metadata are specified. Immutable DB-assigned admission sequence
  plus a sweep high-water bounds concurrent populations. An epoch lock/schema
  fence rejects legacy writers. B-schema freezes independently reachable
  DTO-name/field/API/signature/DDL surfaces only; R2a microcycles make the
  classified-page/progress value rules, exhausted completion, and non-nested
  epoch-2 write behavior GREEN at their own real protocol oracles; B1 remains
  only missing driver/policy REDs. Durable metadata is only
  schema-upgrade progress;
  capacity deferral advances the current iterator, preserves earliest deferred
  next-sweep eligibility in memory, and cannot starve later rowless/other-
  capacity work. Ongoing recovery has no durable scheduler/workflow cursor or
  candidate table.
- B3 resolved: source/read set, write set, exact before/after identities,
  trusted project-bound journal/lock, provisioning location, modes, absence,
  per-destination leaf/absence and ancestor revalidation, no-clobber creation,
  identity-checked replacement, final read-set commit point, stated cooperating-
  writer threat boundary, bounds, fsync, and recovery behavior are frozen.
- B4 resolved: global ownership freezes in R0; each unit's full RED set is
  committed/reviewed before that unit's production changes, with A0/A-schema,
  inert R1a, R1b-P/A/E, B0 preservation at every R1 checkpoint, B-schema, R2a
  protocol GREEN, mandatory real-acceptance B0.5, and B1 missing-policy REDs
  explicitly staged; active
  `docs/DESIGN.md` and distribution scans are included.
- Lifecycle/minimality review resolved: manual provider is command-side only;
  provisioning is atomic/fsynced without a second TTY consent ceremony; no
  merge, generic provider framework, or migration candidate table is added.
- Minimality: every element traces to F1-F8; forbidden expansions are explicit.
- The original freeze was written against
  `b7948567da426e965ec2ae22e2a0f0407fa0be12`. The active isolated implementation
  worktree is now based at `db1204f60b4345d03e292054e8bfcd07c029b155` and
  intentionally contains uncommitted R1a/R1b-P production and test changes.
  The R1 and R1b-P threat models and module boundary above, rather than the
  historical clean-tree statement, govern current implementation review.

**Final self-check verdict: PASS — the corrected replan and current R1b-P
boundary are internally consistent; unfinished units remain gated exactly as
above.**

SHA-256 (UTF-8 content preceding this digest block):
`680c85952ae2985d2346a4f6d473dce3d9616cebd3be1d3ee74711835abb05df`
