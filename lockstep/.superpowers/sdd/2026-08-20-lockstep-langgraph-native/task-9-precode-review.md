# Task 9 pre-code architecture/conformance review

Date: 2026-08-21
Base: `bfaaddd6`
Scope: native direct child calls and `graph` / `include_graph` lowering
Verdict: **GO**, subject to the frozen contracts and release-blocking tests below.

## 1. Review basis and stop decision

The threat model, native design, global plan constraints, Task 9 plan, and Task 8
report were read in full. The current compiler/runtime and the installed
yamlgraph 0.5.22 direct-subgraph implementation were inspected. Existing native
capability tests were also run as a focused baseline (`24 passed`).

There is no unresolved architecture blocker after the rulings in this report.
Production implementation may start. A deviation from any item marked **MUST**
is a Task 9 blocker, not a follow-up.

The deployment remains the threat model's **Local unsandboxed single-user MVP**.
The runner process is TCB for ambient OS-user authority. This task must not claim
that a scope, deadline, namespace, runner selector, or opaque child correlation
string is a sandbox, credential, or security principal.

No child scheduler, child `RunCatalog` row, child credential, ancestry store,
resume token, wrapper invocation, private saver-table query, or auto-resume loop
may be introduced. LangGraph checkpoints plus the existing `EffectLedger` remain
the authoritative lifecycle state.

## 2. Frozen compiler-facing DTOs

The present `WorkflowCatalog.contract_for()` seam is insufficient: it supplies
semantic effects only, and `compile_workflow()` currently discards it. Task 9
**MUST** replace/extend that seam with the following immutable, I/O-free model.
Names below are normative in meaning; minor Python spelling differences are
acceptable only if the same closed fields and invariants are preserved.

```python
YamlgraphStateType = Literal[
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
]

@dataclass(frozen=True)
class ChildWorkflowContract:
    outcomes: tuple[str, ...]
    exports: Mapping[str, ChildArtifactContract]
    non_artifact_writes: tuple[str, ...]
    state_inputs: Mapping[str, YamlgraphStateType] = {}
    state_exports: Mapping[str, YamlgraphStateType] = {}

@dataclass(frozen=True)
class CatalogFile:
    relative_path: str                 # canonical, contained POSIX path
    content: bytes                     # exact canonical compiler output
    sha256: str                        # SHA-256(content)

@dataclass(frozen=True)
class CanonicalCompiledBundle:
    root_relative_path: str
    files: tuple[CatalogFile, ...]      # root included; sorted by path
    bundle_sha256: str                  # domain-separated manifest digest
    compiler_version: str

@dataclass(frozen=True)
class ResolvedChild:
    logical_name: str
    contract: ChildWorkflowContract
    source_definition_sha256: str      # exact resolved workflow definition
    standalone: CanonicalCompiledBundle

@dataclass(frozen=True)
class ResolvedFragment:
    logical_path: str                  # canonical project-relative identity
    source_definition_sha256: str
    fragment: FragmentIR               # parsed, closed, fully frozen IR

@dataclass(frozen=True)
class ResolvedCatalog:
    children: Mapping[str, ResolvedChild]
    fragments: Mapping[str, ResolvedFragment]
```

`ResolvedCatalog` and every nested mapping/sequence **MUST** be structurally
frozen at construction. It contains no `Path`, open stream, callback, loader, or
other ambient authority. Resolution, containment, symlink rejection, duplicate
handling, cycle detection, exact reads, and recursive compilation happen before
pure lowering. Lowering performs no filesystem I/O.

`state_inputs` / `state_exports` are not YAGNI. The plan explicitly requires
contract copies and type mismatch rejection, while yamlgraph direct mode rejects
input/output mapping and communicates only through shared state channels. Their
default is empty for compatibility. Names are fixed identity names at the
standalone child boundary; Task 9 does not add a user-authored per-call mapping
language. A name present in both maps is allowed only with the same type. Reserved
Lockstep names, invalid state names, and any parent/child/generated collision fail
compilation. Parent state may be inferred from these contracts; two consumers of
one external name must agree on its type.

The compilation result **MUST** become a complete generated bundle contract:

```python
@dataclass(frozen=True)
class GeneratedFile:
    relative_path: str                 # non-root generated yamlgraph document
    content: bytes
    sha256: str
    role: Literal["specialized-child"]

@dataclass(frozen=True)
class CompilationResult:
    root_relative_path: str
    recipe_bytes: bytes                # emitted root, retained compatibility API
    generated_files: tuple[GeneratedFile, ...]  # excludes root; sorted by path
    source_map_bytes: bytes
    dependency_manifest_bytes: bytes
    dependency_manifest: DependencyManifest
    digest: str                        # compatibility: SHA-256(recipe_bytes)
    bundle_sha256: str                 # root path/bytes + all generated paths/bytes
    compiler_provenance: CompilerProvenance
```

The root is separate only to preserve the Task 8 `recipe_bytes` API;
`root_relative_path + recipe_bytes + generated_files` is the one complete
executable file set. Duplicate paths, root duplication, non-canonical paths,
path traversal, mismatched hashes, or same-path/different-bytes are constructor
errors. `bundle_sha256` is a domain-separated digest over an ordered manifest,
not a concatenation of file contents and not merely the root hash.

Each dependency-manifest use entry **MUST** contain:

```text
kind: workflow | fragment
logical_name: stable workflow name or canonical fragment path
use_pointer: canonical root-source pointer for this call/inclusion
definition_sha256: exact resolved source definition digest
compiled_sha256: specialized transitive bundle digest for a child, or canonical
                 namespaced expansion digest for a fragment
generated_root: specialized child root relative path, null for a fragment
```

Entries are canonically ordered and cover every direct and transitive child and
every inline/included fragment use. Two call sites for one child therefore have
distinct `use_pointer` entries. Freshness compares the complete resolved read set,
manifest, root, and generated files. A child/fragment mutation makes the parent
stale even when the root workflow bytes did not change.

## 3. Deterministic child specialization

For each call site the compiler emits a deterministic relative child root, for
example under a reserved `generated/children/<call-site-digest>/` namespace. The
path digest **MUST** bind the root definition, source pointer, child definition
and standalone bundle, runner selector, typed state contract, and active ancestor
scope identities. It must not bind an absolute path, mtime, process value, or
unordered mapping representation.

Specialization copies the already validated canonical child DAG. It is a closed
pure transform with these rules:

1. Preserve a bijection of nodes and the exact edge/topology relation. Node-name
   qualification and documented reference rewriting are allowed; adding/removing
   child nodes or edges is not. Pre/scope/post nodes belong to the parent DAG.
2. Namespace all child-private generated state, its terminal outcome channel,
   contract copies, protected interrupt request/result channels, and stable node
   identities by the call site. Rewrite only fields whose yamlgraph semantics are
   documented and parsed. Generic string replacement is forbidden.
3. Add the call scope result channel and active ancestor scope channels to both
   the parent and specialized child schemas. Also add every protected child
   `state_key` / `resume_key` and state-selected input needed by the coordinator
   to the parent bridge schema. This is mandatory because `NativeSnapshot.values`
   is root-state only and yamlgraph filters child-only output.
4. Convert only standalone `kind=manual, runner=null` worker descriptors in the
   called child's own body to `kind=managed` with the exact validated call
   selector and standard managed capabilities. Preserve pinned, verify, decide,
   accept, publish, and already managed execution classes.
5. Recursively rebase descendant generated paths, state namespaces, and ancestor
   deadline keys. A nested call's own declared runner wins for its descendants;
   an outer runner must not overwrite an already specialized nested managed
   descriptor.
6. Re-parse every transformed descriptor with `known_state_keys`, then run the
   recursive generated profile and real yamlgraph compilation. Standalone child
   bytes remain unchanged and standalone worker steps remain manual.

The parent lowering is exactly:

```text
call scope interrupt -> pre bridge -> mode: direct specialized child -> post bridge
```

The pre bridge copies only declared `state_inputs` into call-namespaced child
copies and saves/resets execution context. The post bridge copies only declared
`state_exports`, interprets the specialized child outcome, and restores the
parent context. No child-internal field may become parent routing authority.

The child terminal channel must be namespaced; it must not overwrite the parent
`lockstep_outcome`. At minimum `current_step`, `_loop_counts`, and
`_loop_limit_reached` require save/reset/restore because yamlgraph itself writes
those shared infrastructure keys. Generated workflow-local control keys such as
`lockstep_continue` and child outcome are namespaced instead. Node names are also
qualified so `_loop_counts` entries cannot collide. Sequential post-child effects
must observe the original parent context and must not reference the completed
child scope.

## 4. Native scope, deadline, and exact runner binding

The call scope is the existing protected no-spawn `ScopeDescriptor`; it is not a
child process or timer object. Its unique result key is declared in parent and
specialized child state and is used as the interrupt `resume_key`.

For a call:

- `duration_seconds = timeout_minutes * 60`, or `None` when omitted;
- `runner_selector` is the validated call selector;
- `ancestor_deadline_state_keys` contains all active outer scope result keys in
  deterministic outer-to-inner order;
- the resulting `ScopeResult` stores the sealed absolute deadline and exact
  runtime runner binding digest.

Ordinary effects directly inside that child use only the **innermost** call scope
key in `scope_state_keys`. For a nested call, its `ScopeDescriptor` receives outer
keys in `ancestor_deadline_state_keys`, and effects inside the nested child bind
only the nested result key. This preserves deadline attenuation while allowing a
nested call to select a different runner. Putting every call scope into an
ordinary descriptor would make the current coordinator incorrectly require all
ancestor runner selectors to match.

The compiler binds a selector and descriptor digest, not a live adapter digest.
At runtime the existing coordinator resolves the owner-configured adapter, seals
its `runner_binding_digest` into the scope result/request, takes the minimum of
effect and ancestor deadlines, and revalidates the exact descriptor, scope result,
native lineage, adapter binding, deadline, and request immediately before spawn.
Task 9 should reuse this seam unchanged. A selector mismatch, stale scope digest,
expired deadline, or changed adapter binding rejects before spawn.

## 5. Bundle provenance and recursive profile authority

The current `CompilerProvenance` is bound only to root recipe bytes, and
`check_recipe_full(path)` checks only that root. This is insufficient for a
direct child DAG. Task 9 **MUST** introduce bundle provenance:

```python
@dataclass(frozen=True)
class ProvenanceFile:
    relative_path: str
    canonical_execution_bytes: bytes
    sha256: str
    role: Literal["root", "specialized-child"]

@dataclass(frozen=True, init=False)
class CompilerProvenance:
    files: tuple[ProvenanceFile, ...]   # root + every generated runtime doc
    root_relative_path: str
    bundle_sha256: str
    source_bundle_sha256: str          # CompilationResult.bundle_sha256
    context: Literal["compiler-output", "canonical-match"]
    compiler_version: str
    # constructor remains private/token-gated
```

`canonical_execution_bytes` means the exact canonical bytes admitted by
`StrictRecipeIngress` and later passed to yamlgraph. Compiler-emitted YAML and
ingress-normalized execution JSON are distinct representations; provenance must
not accidentally compare one representation to the other. The trusted compiler
path proves the mapping from emitted file set to the exact canonical execution
file set. `canonical-match` may be issued only after the freshness verifier has
reproduced and byte-matched the root, every generated file, and the complete
dependency read set.

The recursive profile walks the authoritative closed subgraph DAG and associates
each document with its exact provenance file. Every compiler-only scope,
generated dependency, and compiler marker in every document requires matching
bundle provenance. A manual root receives no provenance; a compiler-only marker
in any reachable child is therefore rejected. Extra, missing, reordered,
substituted, or digest-mismatched files fail closed before yamlgraph compilation.

`StrictRecipeIngress` remains the earliest authority boundary for every expanded
document and every complete manual recipe. Owner-reviewed Python/shell grants are
still exact recipe/requirement grants of full `os_user_execution`. Task 9 does
not enable public start of checked-in generated output; that remains the Task 12
release gate. Immediate in-process compiler output may be admitted only through
the exact bundle proof above, never through a path exception or generated flag.

`RecipeBundleStore`, `ValidatedDependencyDAG`, immutable blob capture, read-only
materialization, `AuthorizedRecipe`, and `AuthorizedMaterialization` are the
existing storage seams to reuse. Do not add a child bundle store. Extend capture
to accept the already validated compiler file map/DAG; retain source map,
dependency manifest, and loader-retained source inputs in the bundle when the
public generated-output admission path is completed.

## 6. Frozen fragment contract

The original authoring design allowed executable fragment nodes and required a
post-region actual-delta oracle, while the native Task 9 architecture has no such
region lifecycle object. The minimal conformant Task 9 resolution is:

- generated DSL fragments permit only `passthrough` and closed protected
  `interrupt` nodes whose `lockstep_effect` descriptors are accepted by the
  existing descriptor parser/coordinator;
- in-graph `python`, `tool`, `tool_call`, `verify`, `map`, nested subgraph,
  fragment include, pipeline, and every forbidden autonomous node are rejected;
- `effects.mode=read-only` requires `writes=[]` and every descriptor write union
  to be empty;
- `effects.mode=declared-writes` requires a non-empty canonical list exactly equal
  to the canonical union of `EffectDescriptor.writes` for all reachable protected
  effects. Actual deltas are then enforced at the existing Task 4/7 provider and
  effect commitment boundary on PASS, FAIL, and ERROR;
- a complete manual yamlgraph recipe remains the explicit owner-reviewed escape
  hatch under the existing authority policy.

This prevents graph-native code from bypassing the coordinator/outbox/recovery
boundary. An `os_user_execution` grant would remove a security authority delta in
the local profile, but it would not restore the DSL effect/recovery contract; the
narrow generated-fragment profile is therefore a correctness requirement.

Fragment exits are a non-empty subset of the closed names `{pass, fail, error}`;
`pass` is required for a composable DSL graph block. Missing fail/error exits are
not synthesized and must be structurally unreachable. Every declared exit is
reachable, every reachable path terminates at a declared exit, exit nodes have
no outgoing local edge, and bounded cycles must have a valid local loop exit.
`on` may refer only to declared exits: `pass -> next`; present fail/error routes
default to and may only be `escalate` in v1. Lowering must not invent outcome
routing.

Inline and included fragments are parsed into the same closed `FragmentIR`.
`include_graph` resolution occurs before lowering from a contained canonical
project-relative path with no symlink/traversal/live path retained. Expansion is
one level, has the 1,000-node cap, and is inserted into the parent graph rather
than emitted as a runtime subgraph.

Every local node, edge endpoint, state key, tool identity/reference, loop key and
target, entry/exit, parsed condition state reference, documented `state_key` /
`resume_key`, template state reference, visible step identity, descriptor
state-selector/result key, and artifact handle/reference is deterministically
namespaced. Only documented parsed reference fields are rewritten. Unknown
reference-bearing shapes fail compilation. After expansion the complete root is
re-run through `StrictRecipeIngress`, recursive generated profile, descriptor
validation, and yamlgraph compile.

## 7. Runtime projection and restart/async oracle

Direct semantics are already supplied by the reviewed yamlgraph patch:
`create_subgraph_node(mode=direct)` returns the child `CompiledStateGraph`, the
parent registers it directly, the child is compiled without a saver, and the
parent checkpointer is inherited by LangGraph. Task 9 must not wrap this object.

The compatibility `child_run_id` is an annotation only. Derive it as a
domain-separated SHA-256 of the parent public run ID plus the current public
native subgraph task coordinate. Do not expose `checkpoint_ns`, create a child
row, accept it in `scenario_done`/status/history as a public run ID, or store it
in graph state. `RunCatalog` lookup naturally rejects it. Status/history must use
the existing public `StateSnapshot` task/interrupt APIs.

The restart oracle is two deliberately different tests:

1. **Synchronous SQLite:** park at a real direct-child interrupt, close the app or
   service, reconstruct from the immutable bundle plus existing SQLite saver,
   resume the exact interrupt, and reach the expected parent continuation. Prove
   the scope result and child result survived restart.
2. **Real async memory:** `await NativeApp.ainvoke(...)` parks inside a direct
   child and `await NativeApp.aresume(...)` resumes by exact interrupt ID. Add the
   narrow `aresume` port using `await app.ainvoke(Command(resume=...))`. Do not
   add `AsyncSqliteSaver`, a second runtime, an executor-wrapped sync surrogate,
   or private checkpoint access merely to combine both properties in one test.

## 8. Existing reuse points

- yamlgraph adapter: the only allowed import boundary for yamlgraph/LangGraph,
  direct `CompiledStateGraph`, public recursive snapshots/history, sync SQLite
  saver and memory saver.
- `StrictRecipeIngress`: bounded strict YAML decode, canonicalization, closed
  recursive subgraph DAG, exact executable authority discovery.
- `RecipeBundleStore` / `ValidatedDependencyDAG`: immutable flat DAG capture,
  digest verification, read-only materialization. It is already sufficient.
- effect descriptor parser: closed descriptors, canonical descriptor digests,
  known-state-key validation, scope result schemas.
- `EffectCoordinator` / `EffectLedger`: exact native coordinates, sealed
  `ScopeResult`, deadline minimum, runner binding digest, lineage validation,
  outbox/commitment/recovery. No child-specific fork is needed.
- `GraphRuntime`: one root binding, parent saver, invocation lease, public native
  snapshot/history/resume.
- Task 8 canonical serialization, source maps, diagnostics, semantic contracts,
  and dependency manifest are extended rather than replaced.

## 9. Plan corrections and file-scope ruling

The Task 9 file list is descriptive, not sufficient. Implementation necessarily
may modify `workflow/compiler.py`, `workflow/semantics.py`, fragment IR/schema or
resolver modules, `recipe/authority.py`, `runtime/status.py`,
`runtime/native_models.py`, `recipe/yamlgraph_adapter.py`, and focused tests,
because the required seams do not exist elsewhere. That is not scope expansion;
it is the earliest effective boundary for the planned behavior.

The legacy subcall modules/tests named for deletion were already removed by Task
3. Verify their absence; do not recreate them or add replacement scheduler
aliases. `tests/runtime/test_legacy_test_migration.py` currently forbids
`test_daily_change_recipe.py`, while Task 9 explicitly requires a new native
test with that name. Update the stale guard to forbid only the old subcall test
names/content and permit the new native direct-child test.

Artifact byte publication/export remains Task 10. Task 9 validates artifact
contract existence, naming and missing-export errors, but must not invent a
publication side channel.

## 10. Findings in threat-model format

### T9-F01 / Catalog has semantics but no immutable compiled artifact

```text
ID / Title: T9-F01 / Catalog has semantics but no immutable compiled artifact
Primary class: Correctness
Secondary class (optional): Reliability
Status: Confirmed

Asset(s): A-06, A-07
Invariant(s): SI-10, SI-11, SI-14
Deployment profile and assumption: single-user MVP; compiler is TCB
Attacker persona and capability: P-03 may edit workflow/fragment source
Trust-boundary entry: DF-01 -> DF-02
Preconditions (including configuration and grants): Task 9 call/include lowering

Pre-existing authority:
  Author may supply source bytes but no trusted compiled-child object or live loader.
Authority obtained by exploit:
  None established; current code raises/not implemented.
Authority delta:
  Zero in current code. Wrong future implementation could substitute stale bytes.
Why the same result is unavailable through an authorized operation:
  N/A; primary class is correctness, not security.

Reachable attack trace:
  1. A validated call reaches compile_workflow.
  2. The catalog supplies only ChildWorkflowContract.
  3. compile_workflow deletes the catalog; lowering cannot obtain a child DAG.
  4. A direct child cannot be emitted or freshness-bound.

Impact and blast radius: stale/wrong child graph or inability to compile Task 9
Likelihood: N/A — no security score for zero authority delta
Impact: N/A — no security score for zero authority delta
Risk/Priority: release-blocking correctness gap

Evidence / minimal reproduction: compiler.py `del catalog`; WorkflowCatalog only contract_for
Expected safe behavior (test oracle): immutable ResolvedCatalog + transitive mutation tests
Recommended mitigation at earliest effective boundary: frozen DTOs in §2
Why the mitigation is minimal and sufficient: gives pure lowering exact bytes and digests
Residual risk after mitigation: trusted compiler bugs; deterministic/golden tests apply
Verification test(s): new child DAG, mutation, collision and freshness tests

Stop-rule check:
  Untrusted source cannot receive/mutate the frozen resolved object. Forging it after
  construction requires TCB code/reflection; further recursive hardening stops. This
  finding remains correctness unless a concrete untrusted mutable reference appears.
```

### T9-F02 / Root-only provenance does not authorize or reject the complete DAG

```text
ID / Title: T9-F02 / Root-only provenance does not authorize or reject the complete DAG
Primary class: Correctness
Secondary class (optional): Defense-in-depth
Status: Confirmed

Asset(s): A-02, A-06, A-07
Invariant(s): SI-01, SI-11, SI-14
Deployment profile and assumption: single-user MVP; owner grants remain authoritative
Attacker persona and capability: P-03 controls a manual root and reachable child YAML
Trust-boundary entry: DF-01 -> DF-02
Preconditions (including configuration and grants): manual root references a child

Pre-existing authority:
  Author may edit recipe files; executable effects still need independent grants/gates.
Authority obtained by exploit:
  A compiler-only scope marker can currently evade the root-only profile decision.
Authority delta:
  No demonstrated process/provider/publication authority; scope is no-spawn and manual
  YAML can already request ordinary effects subject to the same runtime authority gate.
Why the same result is unavailable through an authorized operation:
  N/A for security; this violates the compiler/manual language contract.

Reachable attack trace:
  1. Manual root references child.recipe.yaml.
  2. StrictRecipeIngress closes the DAG structurally.
  3. check_recipe_full checks only materialized root bytes.
  4. Child compiler-only scope is not rejected by the intended profile boundary.

Impact and blast radius: incorrect compiler/manual classification for one bundle
Likelihood: N/A — zero demonstrated security authority delta
Impact: N/A — zero demonstrated security authority delta
Risk/Priority: release-blocking correctness gap

Evidence / minimal reproduction: focused negative child-scope test in Task 9 suite
Expected safe behavior (test oracle): manual parent rejects compiler-only marker in child
Recommended mitigation at earliest effective boundary: recursive exact bundle provenance §5
Why the mitigation is minimal and sufficient: one admission/profile walk covers all consumers
Residual risk after mitigation: direct TCB memory compromise is out of scope
Verification test(s): manual-child marker, missing/extra/substituted generated file tests

Stop-rule check:
  Project bytes are untrusted and cross the boundary, so recursive validation is mandatory.
  Once exact frozen provenance is created inside TCB, forging it requires TCB compromise;
  hardening beyond that stops under §11.
```

### T9-F03 / Direct child state is filtered without an explicit bridge

```text
ID / Title: T9-F03 / Direct child state is filtered without an explicit bridge
Primary class: Correctness
Secondary class (optional): Reliability
Status: Confirmed

Asset(s): A-01, A-07, A-08
Invariant(s): SI-04, SI-09, SI-10
Deployment profile and assumption: single-user MVP; public LangGraph APIs are authoritative
Attacker persona and capability: no attacker required; ordinary child execution triggers it
Trust-boundary entry: DF-02 and DF-10
Preconditions (including configuration and grants): direct child parks or writes child-only state

Pre-existing authority:
  None relevant.
Authority obtained by exploit:
  None.
Authority delta:
  Zero; failure is lost state/routing context.
Why the same result is unavailable through an authorized operation:
  N/A; primary class is correctness.

Reachable attack trace:
  1. A direct CompiledStateGraph writes child-only result/scope state.
  2. Parent schema does not declare that channel.
  3. LangGraph filters it and NativeSnapshot.values exposes root state only.
  4. Coordinator cannot resolve the parked descriptor/result or parent context is overwritten.

Impact and blast radius: stuck/misrouted run; restart may make it persistent
Likelihood: N/A — no security score
Impact: N/A — no security score
Risk/Priority: release-blocking correctness/reliability gap

Evidence / minimal reproduction: direct-child public snapshot probe; yamlgraph does not infer resume_key
Expected safe behavior (test oracle): shared declared scope/effect channels plus context restoration
Recommended mitigation at earliest effective boundary: specialization and pre/post bridge §3
Why the mitigation is minimal and sufficient: uses native shared-state semantics, no side store
Residual risk after mitigation: upstream yamlgraph semantic drift; pinned capability tests catch it
Verification test(s): scope key, nested/sequential context, SQLite restart tests

Stop-rule check:
  No untrusted internal object or authority delta exists. Extra redundant runtime stores would be
  correctness debt, not security hardening, and are rejected by the native design.
```

### T9-F04 / Executable fragment nodes bypass the protected effect lifecycle

```text
ID / Title: T9-F04 / Executable fragment nodes bypass the protected effect lifecycle
Primary class: Correctness
Secondary class (optional): Defense-in-depth
Status: Confirmed as an architecture conflict; resolved by §6

Asset(s): A-01, A-02, A-05, A-07
Invariant(s): SI-01, SI-04, SI-07, SI-14, SI-29
Deployment profile and assumption: Local unsandboxed; granted executable is TCB for OS authority
Attacker persona and capability: P-03 authors a fragment; execution still requires owner grant
Trust-boundary entry: DF-01 -> DF-02 -> DF-05
Preconditions (including configuration and grants): generated fragment contains python/tool node

Pre-existing authority:
  Without exact owner grant, no executable authority. With os_user_execution grant, the process
  already has full ambient OS-user authority in this deployment.
Authority obtained by exploit:
  No additional OS authority after the grant, but graph-native execution bypasses effect outbox,
  exact request commitment, recovery and declared-write accounting.
Authority delta:
  Zero for OS authority in the granted Local unsandboxed profile; nonzero workflow-state
  correctness/recovery deviation, so primary class is not Security.
Why the same result is unavailable through an authorized operation:
  Protected interrupt effects provide the authorized/recoverable operation instead.

Reachable attack trace:
  1. Fragment embeds an executable yamlgraph node.
  2. Compiler expands it directly into the graph.
  3. LangGraph invokes it without EffectCoordinator/outbox.
  4. Actual mutation cannot be correlated to the declared fragment effect on every exit.

Impact and blast radius: incorrect effects, duplicate/unrecoverable execution for one local run;
with full owner grant, ambient filesystem blast radius is already accepted
Likelihood: N/A — zero security delta in named deployment
Impact: N/A — primary class correctness
Risk/Priority: release-blocking correctness gap

Evidence / minimal reproduction: current runtime has per-effect gates but no fragment-region oracle
Expected safe behavior (test oracle): generated executable node types rejected; writes equal descriptor union
Recommended mitigation at earliest effective boundary: closed fragment IR/profile §6
Why the mitigation is minimal and sufficient: all effects reuse the existing durable boundary
Residual risk after mitigation: complete manual recipes with owner grant remain explicit TCB escape hatch
Verification test(s): forbidden node tests, effect-union mismatch on all outcomes, manual grant regression

Stop-rule check:
  A compromised granted local runner already has ambient OS-user authority, so claims that another
  in-process check confines it are rejected by the stop rule. The generated-language lifecycle
  restriction remains a correctness requirement.
```

## 11. Blockers, non-blocking cleanup, and GO gate

### Unresolved blockers

None. The missing decisions are frozen in §§2–7.

### Release-blocking implementation conditions

- immutable complete `ResolvedCatalog` and generated-file/result DTOs;
- deterministic transitive specialization with topology-preservation oracle;
- typed state contract and root-visible protected state bridge;
- correct innermost-scope/ancestor-deadline behavior and parent context restore;
- exact recursive bundle provenance/profile and transitive freshness;
- fragment CFG/namespace/effect profile from §6;
- sync SQLite restart plus true async memory `aresume` oracle;
- no legacy child lifecycle state or imports.

### Non-blocking cleanup/compatibility choices

- retain `CompilationResult.digest` as the root SHA compatibility field while new
  code uses explicit `bundle_sha256` for bundle identity;
- already deleted Task 3 subcall files require absence assertions only;
- file-list additions in §9 are administrative plan corrections, not design work;
- source-map presentation details may evolve if the final/local identity remains
  deterministic and both identities remain observable.

### Required verification matrix before claiming Task 9 complete

1. Contract input/export identity copy; missing export; same-name type conflict;
   reserved/generated collision.
2. One native `mode: direct` node with parent scope/pre/post; no wrapper/scheduler.
3. Standalone child byte stability; specialized node/edge topology isomorphic;
   two calls and nested calls have disjoint deterministic paths/state.
4. Standalone manual worker remains manual; called worker becomes managed; nested
   declared runner wins; exact runtime runner binding mismatch rejects pre-spawn.
5. Scope `resume_key` exists in root and child; nested deadline is the minimum;
   sequential post-child effect does not inherit scope; parent context restored.
6. Root/child/fragment/generated mutation, missing file, extra file, path collision,
   and transitive child change all fail freshness/provenance deterministically.
7. Fragment pass-only positive case; declared fail/error routing; unreachable exit,
   nonterminating path, nested include/subgraph, forbidden executable node,
   reference rewrite, namespace collision, and effect-union mismatch negatives.
8. Manual root plus compiler-only child scope rejects recursively.
9. Real SQLite restart inside direct child and real memory async pause/`aresume`.
10. Opaque `child_run_id` is non-addressable and reveals no namespace; no child row.
11. New daily native direct-child test is allowed; all stale `_subcall*`, RunIndex
    child, credential, scheduler, wrapper, and old test names remain absent.

With that matrix GREEN, the Task 9 architecture conforms to the reviewed native
design and threat-model stop rule.
