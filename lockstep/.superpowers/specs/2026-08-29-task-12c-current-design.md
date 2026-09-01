# Task 12C current design: architecture ratchet, real templates, and installed contract

**Status:** Active controlling Task 12C contract. The user directed the agent to
achieve the established final goal and to validate specifications,
implementation ranges, and corrections through independent reviews without
requesting intermediate user approvals. Tasks 1–3 and their corrective ranges
are independently accepted. The current §§4.3–§4.5 Task 3A/Task 4 interface
amendment must reach independent C0/I0/M0 before Task 3A RED; it grants no
retroactive or later-phase authority.

**Reference baseline:** `4674e43fa1ffef1b9013f29345b2c7934808131e`

**Authority:** This spec supersedes the stale Task 12C create-file list,
`recipe init --template`
grammar, atomic full-bundle language, scope-only packaged-template assumption,
and old sequencing in
`.superpowers/plans/2026-08-20-lockstep-langgraph-native.md`. That stale plan
text remains historical evidence rather than implementation authority.

The binding predecessor documents are:

- `.superpowers/specs/2026-08-20-lockstep-threat-model.md`;
- `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12-corrected-replan.md`;
- `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12-god-method-remediation-plan.md`;
- `.superpowers/reviews/2026-08-29-task-12a5-proportionality.md`.

The controlling dependency order remains:

1. repository-wide architecture ratchet and any feasible remediation;
2. DSL artifact export and real authority-bearing built-in templates;
3. complete tests-only installed-contract Gate D RED;
4. minimal installed-contract GREEN;
5. full source, clean-wheel, staged-plugin, and installed reviews, followed by
   a stop before publication.

## 1. Exact current baseline and authority boundary

| Surface | Current fact at `4674e43` |
| --- | --- |
| Production scan root | All 105 tracked `*.py` files under `engine/src/lockstep` |
| Production population | 1,566 lexical functions and 316 classes |
| Current structural gate | `engine/tests/architecture/test_no_god_methods.py` checks 20 named historical methods plus authoring functions; no repository-wide class/file/composite manifest exists |
| Legacy method metrics | Failure begins at cyclomatic 16, cognitive 26, nesting 5, or 25 unique syntactic `ast.dump(call.func)` forms; physical line counts are not measured or gated |
| Active authoring | 8 production modules and 15 focused test modules |
| Last production evidence | 1,708 passed, 1 skipped; the historical physical-length warning is obsolete under the clarified non-line-based contract |
| Packaged templates | Exactly `reviewed-change` and `parallel-review`; seven YAML resources; currently scope-only and authority-free |
| Artifact runtime | Immutable child artifact descriptor, registry, provenance, parent import, bearer consent, publication, and restart paths already exist for manual YAMLGraph |
| Artifact DSL gap | `step.artifact` is an unclosed message mapping and never becomes a runtime artifact descriptor/export |
| Managed-step gap | Call specialization turns a manual step into `kind=managed` but does not supply Codex's required `brief` and `snapshot` inputs or full capabilities |
| Legacy runner | `engine/src/lockstep/runtime/runners.py` and its obsolete focused test `engine/tests/test_runners.py` remain active residue to retire |
| Estimate residue | Public output still uses `peak_parallel_subcalls` |
| Host residue | Active manifests, launcher, docs, skills, and tests still use `LOCKSTEP_RUNNER` |
| Gate D | `engine/tests/test_installed_contract.py` does not exist |

Task 12A.5 / Gate P and the selected `simplify-with-write` remediation are
closed at the baseline. Their final product/proportionality, architecture/SRP,
and threat/reliability reviews returned Critical/Important/Minor zero. Task 12C
is now underway under this separate active contract.

The current public authoring grammar is preserved exactly:

```text
recipe init NAME
recipe compile NAME
recipe check [NAME | --all]
recipe diff NAME
recipe render NAME --view workflow|generated
recipe estimate NAME [--json]
template list
template show TEMPLATE NAME
template init TEMPLATE NAME
```

MCP remains `recipe_init`, `recipe_compile`, `recipe_check`, `recipe_diff`,
`recipe_render`, `recipe_estimate`, `template_list`, and `template_show`.
Template installation is CLI-only. There is no MCP template init, `--format`,
path positional, `recipe init --template`, grammar alias, or custom template
path. The current implementation plan is active; each remaining RED/GREEN range
still requires its specified independent review boundary.

## 2. Product contract, ownership, and deployment profile

Task 12C must prove three first-class authoring/runtime paths:

1. `reviewed-change`: manual plan → manual tests → manual implementation →
   pinned pytest verification → production-adapter managed Codex child-review
   lifecycle with a controlled owner-selected executable → immutable,
   hash/provenance-bound exported artifact → owner-bearer acceptance and
   publication, including close/reopen recovery;
2. `parallel-review`: two genuinely overlapping production-adapter managed
   Codex child-review lifecycles with controlled executables,
   two distinct immutable artifacts, native `join: all`, then two independently
   bearer-authorized publications;
3. marker-free manual YAMLGraph: no DSL marker, template marker, or generated
   provenance requirement; it remains checkable, estimable, startable,
   observable, and recoverable from source, clean-wheel, and staged-plugin
   surfaces.

Ownership remains closed:

- workflow schema admits exact DSL shapes and diagnostics;
- semantic validation owns logical identities, export/write consistency,
  call mappings, destination collisions, and child contract compatibility;
- lowering owns deterministic projection into existing YAMLGraph descriptors;
- the immutable authoring plan owns transitive capture and child-first compile;
- the per-file publisher owns serialized atomic durable replacement of each
  file, never bundle rollback/recovery;
- owner provisioning selects the closed `codex`/`pinned` bindings and grants;
- runtime admission/currentness, provider commitment, artifact registry,
  consent, publication, delivery, and recovery retain their existing owners;
- packaging owns active installed bytes, not workflow authority.

The deployment profile is exactly **Local unsandboxed single-user**. Managed
and pinned processes execute with ambient OS-user authority and enter the TCB
for that authority. Workspace preparation, capability labels, permission
profiles, process supervision, and sandbox attestations are integrity and
lifecycle mechanics in this profile; they are not security confinement. This
design makes no constrained-runner, broker, or sandbox guarantee. A real
constrained-runner profile would require separately proven isolation and is
outside Task 12C.

No configuration, manifest variable, template byte, recipe source, report text,
artifact digest, run ID, or `PASS` string grants authority. Only the existing
owner-selected runtime grant can authorize OS-user execution, and only a fresh
exact bearer can authorize the publication commitment it names.

## 3. Frozen architectural constraints

The complete remaining Task 12C range has these non-negotiable constraints:

- zero new durable schemas, state machines, lifecycle owners, or lock families;
- zero new provider abstractions, schedulers, compatibility aliases, or
  dependency patches;
- no physical line-count metric, patch-size measurement, gross-addition,
  net-growth, analyzer-size cap, or per-phase LOC budget is collected or used
  as a requirement, quality gate, review signal, or stop condition;
- implementation size is governed by responsibility cohesion, the threat
  model, the named invariants, complete tests, and the god-object adjudication
  metrics rather than by patch line counts;
- review must reject needless abstractions, duplicate ownership, unrelated
  cleanup, or weakened evidence regardless of implementation size.

## 4. Phase 1 — deterministic repository-wide architecture ratchet

### 4.1 Internal owners and YAGNI boundary

The ratchet is test-owned architecture enforcement with seven focused internal
components and exactly seven one-role modules:

1. **source index** — tracked-file enumeration, AST parse, containment, stable
   identities, exact source spans;
2. **legacy metrics** — unchanged cyclomatic, cognitive, nesting, and
   syntactic fan-out algorithms;
3. **call resolver** — import/alias/receiver resolution and stable callsites;
4. **domain/lifecycle propagation** — primitive rule sets, lifecycle table,
   SCC fixed point;
5. **candidate policy** — frozen per-kind metric schemas, signals, and hard
   adjudication triggers;
6. **manifest verifier** — exact schema, digests, gates, reviews, expiry, and
   stale-entry rejection;
7. **diagnostics** — stable ordering and rendering only, with no analysis
   policy.

The modules are respectively `architecture_source_index.py`,
`architecture_legacy_metrics.py`, `architecture_call_resolver.py`,
`architecture_domain_lifecycle.py`, `architecture_candidate_policy.py`,
`architecture_manifest_verifier.py`, and `architecture_diagnostics.py` under
`engine/tests/architecture/`. Their only allowed internal import edges are:

```text
legacy_metrics -> source_index
call_resolver -> source_index
domain_lifecycle -> source_index, call_resolver
candidate_policy -> source_index, legacy_metrics, domain_lifecycle
manifest_verifier -> source_index, candidate_policy
diagnostics -> source_index, candidate_policy, manifest_verifier
```

No reverse edge, cycle, or other import between analyzer role modules is
allowed; standard-library and existing test-dependency imports remain subject
to the frozen dependency budget. The test
entrypoint composes the seven public module functions; diagnostics renders
already computed immutable results and does not orchestrate analysis. No one
module or class may own any two of indexing, call resolution, candidate policy,
and manifest I/O. Every function and every one-hop/class/file aggregate in all
ratchet test support is itself checked by the complete hard-adjudication gate
with zero exceptions. Task 12C does not create a reusable
production package, public API, CLI, MCP tool, skill, or generic lint framework.
Whether the ratchet deserves a reusable tool is an explicit final-roadmap
reevaluation item after Task 12 and requires later user approval.

### 4.2 Scan, identities, spans, and containment

The production scan is exactly every tracked `*.py` beneath
`engine/src/lockstep`, with no name/decorator/file exclusion. It indexes every
`FunctionDef`, `AsyncFunctionDef`, and `ClassDef`; nested named definitions are
separate entities. Stable identity is:

```text
relative_posix_path::lexical_qualified_name
```

Aggregate identities are exact: a one-hop aggregate is its root function
identity plus `::@one_hop`; a file aggregate is
`relative_posix_path::@file`. These suffixes are reserved and cannot be lexical
definition names.

Two definitions with the same stable identity are an analyzer error; occurrence
suffixes are forbidden because duplicate lexical definitions are ambiguous and
shadow at runtime. Traversal is normalized path, then lexical AST order, then
identity. A source span begins at the earliest decorator line when decorators
exist, otherwise at `lineno`, and ends at `end_lineno`; its digest hashes exact
unmodified UTF-8 file bytes including original line terminators. Decorators,
defaults, annotations, bases, keywords, and class bodies participate in the
semantic dependency digest even when the legacy body-complexity metric excludes
them.

Containment is explicit: module→top-level definition, class→direct lexical
method/class, and function→nested definition. Nested bodies never inflate their
lexical parent's legacy complexity or syntactic fan-out; they receive their own
metrics. File definition count includes all lexical definitions. Class method
count includes only direct lexical function/async-function children; inherited
and nested functions are not double-counted.

Lambdas are not separately indexed. Their parameters/body form a nested lexical
binding frame. Every call, import/reference dependency, mutation, effect
domain, lifecycle transition, and unresolved site in a lambda is attributed in
this exact order: nearest enclosing indexed `FunctionDef`/`AsyncFunctionDef`;
otherwise nearest enclosing indexed `ClassDef`; otherwise the file aggregate.
Nested lambdas do not change that search order. Lambda bodies remain pruned only
from the frozen legacy complexity and legacy syntactic fan-out algorithms.
Their exact source spans and AST dumps participate in the selected owner's
source/aggregate and semantic dependency digests, so no lambda behavior is
invisible or assigned a separately exceptable entity identity.

A lambda attributed directly to a class creates a class-local cohesion-evidence
vertex keyed `@lambda:NNNN`, where `NNNN` is its four-digit AST-preorder ordinal
within that class; overflow is an analyzer error. This key is evidence only,
not an indexed entity or manifest identity. Its read/mutated `self`/`cls` fields
and resolved intra-class calls create the same undirected cohesion edges as
direct methods; an unconnected class-lambda evidence vertex remains its own
component. Its mutations enter `mutable_fields`; its calls/dependencies enter
the class semantic digest, propagated domain/transition/lifecycle unions, and
cohesion evidence. It does not increment method/public-method/definition count.
A file-attributed lambda analogously enters the file aggregate semantic digest,
and propagated domain/transition/lifecycle unions without incrementing
definition count or creating a definition-dependency vertex.

Every `Import` or `ImportFrom` statement has identity
`relative_posix_path::import:NNNN`, where `NNNN` is its four-digit AST-preorder
ordinal across the complete file, including imports in nested named/lambda
scopes; more than 9,999 imports is an analyzer error. Its closed record has
exactly `identity`, `owner`, `kind` (`import` or
`from`), nullable literal `module` (null for `import` and only a relative-only
`from . import` form), nonnegative relative `level` (zero for `import`), ordered
`aliases` of exact `name`/nullable `asname`, ordered normalized
`targets` or external labels, `span_sha256`, and `import_semantic_sha256`. The
digest hashes UTF-8 canonical JSON of every preceding field except itself with
sorted keys, compact separators, and no trailing newline. The record and digest
participate in the owning entity, class/file closure, and semantic dependency
digests.

### 4.3 Exact call resolution and stable unresolved callsites

Every canonical analyzer byte sequence in §§4.3–4.7 uses exactly
`json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
separators=(",", ":")).encode("utf-8")` with no trailing newline. This single
serializer governs source-population evidence, the primitive and lifecycle
objects, and every semantic dependency payload.

Calls are enumerated in AST preorder for each selected owner: decorators,
signature, and body for an indexed entity; module body for the file aggregate.
The walk prunes nested named definition/class bodies and descends into a lambda
only when §4.2 assigns that lambda to the current owner. Each call receives the
owner identity followed by `::call:` and its four-digit zero-padded preorder
ordinal; more than 9,999 owner-attributed calls is an analyzer error. Resolution
is closed and deterministic:

- `Name(...)` follows Python lexical binding: the nearest scope containing a
  parameter, named function/class definition, import alias, or any Store/Del is
  local for the complete scope unless an exact
  `global` redirects it to module scope or `nonlocal` redirects it to the
  nearest enclosing function binding. Duplicate declarations, declaration
  after use, missing redirected binding, and global/nonlocal Store/Del make the
  name unresolved. Only then may it resolve to a lexical definition, exact
  import binding, or effect-free builtin;
- `module.symbol(...)` resolves only when `module` is an exact import binding;
- `ClassName.method(...)` resolves when the class is uniquely indexed;
- `self.method(...)` and `cls.method(...)` resolve against the current class,
  then uniquely resolved in-repository bases; `super().method(...)` uses those
  bases in declared order and rejects ambiguity;
- a local receiver resolves only when it has one immutable assignment from a
  uniquely resolved constructor or one uniquely resolved class annotation and
  is never rebound, deleted, passed by reference, captured by a nested scope,
  or assigned conditionally;
- an inline `ResolvedClass(...).method(...)` receiver resolves to that class;
  a `self` field receiver resolves only when all class-wide assignments bind it
  to the same uniquely resolved constructor and no other Store/Del exists;
- a symbol alias resolves only for one direct `alias = resolved_symbol`
  assignment with no later Store/Del/AugStore or closure write;
- an annotated-parameter dependency may bind a `self` field only in a direct
  `__init__` method when the annotation is a non-string, nongeneric exact
  `Name`/`Attribute` resolving to one indexed class; the parameter has no
  Store/Del after argument binding and no use except exactly one unconditional
  top-level `self.field = parameter`; the field has no other Store/Del/AugStore
  anywhere in the class or resolved subclasses; and neither parameter nor field
  is returned, yielded, stored elsewhere, passed as an argument, captured,
  aliased, or used except as the receiver of a resolved call/read. Otherwise
  the field receiver is unresolved;
- decorators and base-class references use the same import/symbol rules;
- dynamic attributes, parameter receivers without the exact annotation rule,
  star imports, reflective lookup, ambiguous inheritance, and every other call
  are unresolved.

An immutable binding must be a single unconditional definition in its owning
scope. A binding in `if`, `for`/comprehension, `while`, `try`/`except`/`finally`,
`with`, `match`, a conditional expression, short-circuit expression, lambda,
or assignment expression is conditional. Multiple Store contexts, any Del or
AugStore, exception-target cleanup, pattern capture, loop target, nested-scope
write, or mutually exclusive branch assignment makes it non-immutable. Lexical
shadowing never falls through to an outer binding once Python marks the name
local. These rules also govern aliases and receiver variables.

An unresolved call is never silently pure or classified by its spelling. It is
recorded by stable callsite identity, source coordinate, and AST dump. Candidate
evaluation does not run until the reference population has zero unresolved
calls: each site must become statically resolvable, join an exact reviewed
effect-free target allowlist, or receive an exact callsite-keyed primitive row
with an explicit semantic target label. Target rows and callsite rows are
disjoint, and a source/ordinal/semantic-digest change invalidates a callsite row.
There are no substring, fuzzy-name, regular-expression, or “looks like I/O”
rules.

`ResolutionIndex` additionally binds the exact source population and exposes
immutable literal evidence for every callsite without exposing mutable AST
objects or changing `ResolvedCall(callsite, target)`. The exact frozen/slotted
records are:

```python
@dataclass(frozen=True, slots=True)
class PositionalLiteralEvidence:
    index: int
    type: str
    value: None | bool | int | str

@dataclass(frozen=True, slots=True)
class KeywordLiteralEvidence:
    name: str
    type: str
    value: None | bool | int | str

@dataclass(frozen=True, slots=True)
class CallsiteEvidence:
    callsite: str
    owner: str
    line: int
    column: int
    positional: tuple[PositionalLiteralEvidence, ...]
    keywords: tuple[KeywordLiteralEvidence, ...]

@dataclass(frozen=True, slots=True)
class ResolutionIndex:
    calls: Mapping[str, ResolvedCall | UnresolvedCall]
    aliases: Mapping[str, str]
    receivers: Mapping[str, str]
    dependencies: Mapping[str, ResolvedDependency | UnresolvedDependency]
    reference_source_sha256: str
    call_evidence: Mapping[str, CallsiteEvidence]
```

`reference_source_sha256` hashes canonical JSON rows
`{"path": path, "source_sha256": digest}` ordered by normalized path and must
equal the primitive envelope's separate `reference_source_sha256` field.
`call_evidence` has exactly the same keys and iteration order as `calls`; the
record callsite equals its key and owner equals the callsite prefix. Line is
positive and column nonnegative. Only direct `ast.Constant` values whose exact
type is `NoneType`, `bool`, `int`, or `str` are recorded with JSON type labels
`null`, `bool`, `int`, and `str`; booleans are not integers. Positional records
are ordered by increasing AST argument index and keyword records remain in AST
source order. Absence means missing, spread, nonliteral, or unsupported evidence
and cannot satisfy a lifecycle discriminant. Call ownership and ordinals remain
solely resolver-owned. The primitive envelope's existing `callsite_evidence`
remains its distinct hash-attestation array and is never substituted for
`ResolutionIndex.call_evidence`.

### 4.4 Legacy metrics, primitives, and lifecycle table

Legacy cyclomatic, cognitive, and nesting metrics retain the exact current
`test_no_god_methods.py` AST algorithm. `legacy_syntactic_fanout` separately
retains the exact count of unique `ast.dump(call.func,
include_attributes=False)` forms in the function body while pruning nested
function/class/lambda scopes. It is never replaced by resolved fan-out.
`resolved_fanout` is the separate count of unique normalized call targets.

The version-1 effect-domain primitive table is a strict checked-in canonical
JSON envelope with exactly `schema_version`, `reference_source_sha256`,
`callsite_evidence`, and `rows`; `schema_version` is integer `1`.
`callsite_evidence` contains exactly one closed
`{"selector","owner_source_sha256","call_ast_sha256"}` hash-attestation record
for every callsite row in the same order, with no entity/orphan/duplicate row.
Primitive rows are sorted by `selector_kind` then `selector`. The primitive
digest hashes this complete envelope, not a bare rows array.
Each row has exactly `selector_kind` (`entity` or `callsite`), `selector` (a stable
entity/normalized external target or stable callsite identity respectively),
`semantic_target`, and ordered unique `domains`. An effect primitive may map to
a set of one or more domains; forcing a call into one domain is forbidden. The
resolver accepts an indexed-entity row as used only when `selector` and
`semantic_target` are that same exact indexed identity; Task 4 applies its
domains directly to that entity. An external entity row applies to every exact
resolved invocation owner. A callsite row applies only to its exact owner.
Resolver validation never performs domain propagation.
The closed domains are:

1. `decode/validate`;
2. `planning/transformation`;
3. `filesystem-read`;
4. `filesystem-write`;
5. `durable-state`;
6. `synchronization`;
7. `external-process/provider`;
8. `authority/commitment`;
9. `lifecycle-control`;
10. `projection/output`.

Lifecycle transitions are an independent checked-in canonical JSON table; an
effect-domain row and a lifecycle row may both match the same entity/callsite,
and neither suppresses the other. A lifecycle row has exactly `binding_kind`,
`binding`, `target`, `discriminant`, and `transition_id`:

- an `entity` binding names one stable indexed entity or normalized external
  target whose every invocation has the same unambiguous transition; its
  discriminant is exactly
  `{"kind":"none"}`;
- a `callsite` binding names one stable callsite and its exact resolved target;
  its discriminant is a closed object with exactly `kind`, `positional`, and
  `keywords`; `kind` is exactly `literal-arguments`. Positional entries are
  ordered unique closed objects with exactly `index`, `type`, `value`;
  keyword entries are lexicographically ordered unique objects `name`, `type`,
  `value`. Type is exactly `null`, `bool`, `int`, or `str`; value must have that
  exact JSON type and equal the corresponding direct AST constant at the
  callsite. Integers exclude booleans. Neither list may contain spreads, and at
  least one list is nonempty.

The analyzer rejects duplicate bindings, target mismatch, nonliteral or changed
discriminants, and more than one matched transition. A generic target whose
transition is not unambiguous from an entity invariant or the frozen callsite
discriminant receives no lifecycle label; spelling, control-flow guesses, and
return-value inference cannot assign one. Version 1 admits only these transition
IDs/clusters:

| Cluster | Transition ID | Exact from → to |
| --- | --- | --- |
| `owner/provisioning` | `owner.capture` | `absent → captured` |
| `owner/provisioning` | `owner.replace` | `captured → captured` |
| `owner/provisioning` | `owner.revoke` | `captured → revoked` |
| `admission/commitment` | `admission.admit` | `planned → admitted` |
| `admission/commitment` | `admission.park` | `admitted → parked` |
| `admission/commitment` | `commitment.hold` | `admitted → held` |
| `admission/commitment` | `commitment.commit` | `held → committed` |
| `process-execution` | `process.prepare` | `absent → prepared` |
| `process-execution` | `process.launch` | `prepared → launching` |
| `process-execution` | `process.running` | `launching → running` |
| `process-execution` | `process.terminal` | `running → terminal` |
| `process-execution` | `process.indeterminate` | `{launching,running} → indeterminate` |
| `process-execution` | `process.cancel` | `{prepared,launching,running} → cancelled` |
| `artifact/acceptance` | `artifact.register` | `declared → registered` |
| `artifact/acceptance` | `artifact.materialize` | `registered → materialized` |
| `artifact/acceptance` | `consent.issue` | `pending → issued` |
| `artifact/acceptance` | `consent.redeem` | `issued → redeemed` |
| `publication` | `publication.prepare` | `absent → prepared` |
| `publication` | `publication.apply` | `prepared → applied` |
| `publication` | `publication.rollback` | `prepared → rolled-back` |
| `delivery` | `delivery.pending` | `absent → pending` |
| `delivery` | `delivery.deliver` | `pending → delivered` |
| `recovery/watch` | `recovery.claim` | `eligible → claimed` |
| `recovery/watch` | `recovery.defer` | `claimed → eligible` |
| `recovery/watch` | `recovery.acknowledge` | `claimed → acknowledged` |
| `authoring-publication` | `authoring.plan` | `absent → planned` |
| `authoring-publication` | `authoring.replace` | `planned → replaced` |
| `authoring-publication` | `authoring.directory-durable` | `replaced → directory-durable` |

A lifecycle row chooses exactly one transition ID from this table. Domains come
only from the separate effect-domain rows, so transition cluster and effect
domains coexist without either being inferred from the other. Canonical JSON
uses UTF-8, sorted keys, compact separators, no trailing newline, and rows sorted
by binding kind, binding, target, canonical discriminant bytes, then transition
ID. `lifecycle_digest` hashes the entire object containing schema marker
`lockstep.architecture-lifecycle/v1`, the exact transition vocabulary above,
and all binding rows; changing a discriminant or ordering rule changes it.
SHA-256 digests of the primitive table, effect-free allowlist, lifecycle table,
metric schema, threshold policy, and analyzer version appear in every report
and the exception manifest. Any data change increments the ratchet rule version.
This is analyzer vocabulary over existing calls, not a new runtime lifecycle or
state machine.

The lifecycle object has exactly `schema`, `transitions`, and `rows`, with
`schema == "lockstep.architecture-lifecycle/v1"`. Each transition record has
exactly `cluster`, `transition_id`, ordered nonempty `from`, and `to`; the array
is byte-for-byte the table order above, with every single source state expressed
as a one-item array. Each binding row has exactly `binding_kind`, `binding`,
`target`, `discriminant`, and `transition_id`. Entity rows require
`target == binding` and `{"kind":"none"}`. Callsite rows require the closed
`literal-arguments` discriminant described above. Positional entries are unique
and increasing by `index`; keywords are unique and sorted by `name`; their
combined population is nonempty. Rows are sorted by binding kind, binding,
target, canonical discriminant bytes, then transition ID. Duplicate bindings,
orphan selectors, vocabulary drift, target mismatch, absent/nonliteral/changed
literal evidence, or more than one matched transition blocks the pass.

### 4.5 SCC fixed-point propagation and semantic dependency digest

The resolved call graph has directed edge owner→callee. Direct domains come
only from matching effect-domain rows; direct lifecycle transitions come only
from matching independent lifecycle rows.
Collapse strongly connected components; within each SCC union every member's
direct sets, then propagate callee sets to callers in reverse topological order
until a fixed point. Effect domains, transition IDs, and transition-derived
lifecycle clusters propagate as distinct ordered sets. Thus a thin delegating
owner receives the domains, exact transitions, and lifecycle clusters of its
local dependencies. Unknown sites block this pass.

Each entity has `semantic_dependency_sha256`, the SHA-256 of canonical JSON
containing its identity, exact source SHA, decorator/base identities, normalized
import identities/records/digests and immutable aliases, ordered callsite
identities and resolved targets,
direct and propagated domain sets, direct/propagated transitions and propagated
lifecycle clusters, containment identities, primitive/allowlist/lifecycle/threshold
digests, metric `schema_digest`, and analyzer/rule version. Changing an import
target/record, callee binding,
receiver resolution, decorator, base, domain, lifecycle row, or rule data
therefore expires review even when source text of the caller is unchanged.

The one-hop aggregate semantic digest hashes canonical JSON of its aggregate
identity, ordered member identity/semantic-digest pairs, propagated sets, and
all rule/schema digests. The file aggregate semantic digest hashes canonical
JSON of its aggregate identity, exact file SHA-256, every contained definition
identity/semantic digest in full AST preorder, every import identity/semantic
digest in import order, propagated sets, and all rule/schema digests. These are
the `semantic_dependency_sha256` values for one-hop/file manifest entries.

The sole top-level public entrypoint remains
`propagate_semantics(index, resolutions, primitives, lifecycle, *,
digest_inputs) -> SemanticIndex`. `SemanticDigestInputs` is frozen/slotted and
has exact fields `allowlist_digest`, `schema_digest`, `threshold_digest`,
`analyzer_version`, and `rule_version`; the three digests are strict lowercase
SHA-256 and the versions are nonempty exact strings. Task 4 hashes the complete
strict primitive envelope and complete lifecycle object itself and performs no
hidden file reads. Task 5 owns the actual metric-schema and threshold artifacts
and supplies their digests.

The exact frozen/slotted outputs are:

```python
@dataclass(frozen=True, slots=True)
class SemanticDigestInputs:
    allowlist_digest: str
    schema_digest: str
    threshold_digest: str
    analyzer_version: str
    rule_version: str

@dataclass(frozen=True, slots=True)
class EntitySemantics:
    identity: str
    direct_domains: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    direct_transitions: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str

@dataclass(frozen=True, slots=True)
class FileSemantics:
    identity: str
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str

@dataclass(frozen=True, slots=True)
class OneHopSemantics:
    identity: str
    root: str
    members: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str

@dataclass(frozen=True, slots=True)
class SemanticIndex:
    entities: Mapping[str, EntitySemantics]
    files: Mapping[str, FileSemantics]
    primitive_digest: str
    lifecycle_digest: str
    digest_inputs: SemanticDigestInputs
```

All mappings are `MappingProxyType` and all nested collections are tuples.
`entities` follows source-index order; file identities are `path::@file` in
path order. All set-valued tuples use the frozen vocabulary order.

Task 4 does not choose one-hop membership. The pure
`SemanticIndex.build_one_hop(*, root, members)` method accepts Task 5's explicit
ordered selection, requires a nonempty unique same-file sequence whose every
member is present in `SemanticIndex.entities`, with root first and remaining
identities strictly sorted by stable identity, and never discovers, removes, or
adjudicates helpers. Its three propagated tuples are the vocabulary-ordered
unions of those supplied members only. Its identity is
`root + "::@one_hop"`. Keeping this as a method preserves the
one-top-level-public-entrypoint ownership gate.

Every digest payload is a closed object with exactly the following keys and
nested shapes; no additional key is accepted. The entity payload is:

```json
{
  "schema": "lockstep.architecture-entity-semantics/v1",
  "identity": "stable entity identity",
  "source_sha256": "exact entity span digest",
  "imports": [{
    "identity": "…", "owner": "…", "kind": "import|from",
    "module": null, "level": 0,
    "aliases": [{"name": "…", "asname": null}],
    "targets": ["…"], "span_sha256": "…",
    "import_semantic_sha256": "…"
  }],
  "aliases": [{"binding": "…", "target": "…"}],
  "receivers": [{"binding": "…", "target": "…"}],
  "calls": [{"callsite": "…", "target": "…"}],
  "dependencies": [{
    "reference": "…", "owner": "…", "kind": "decorator|base|metaclass",
    "target": "…"
  }],
  "containment": ["direct child identity"],
  "direct_domains": [], "propagated_domains": [],
  "direct_transitions": [], "propagated_transitions": [],
  "propagated_lifecycle_clusters": [],
  "rule_inputs": {
    "allowlist_digest": "…", "primitive_digest": "…",
    "lifecycle_digest": "…", "schema_digest": "…",
    "threshold_digest": "…", "analyzer_version": "…",
    "rule_version": "…"
  }
}
```

Imports are complete current `ImportRecord` projections in file AST order
filtered to the exact owner. Alias and receiver records are sorted by binding;
calls and dependencies preserve their resolver mapping order; containment is
direct children in source-index order.

Every `binding` value is the verbatim key from the corresponding
`ResolutionIndex.aliases` or `ResolutionIndex.receivers` mapping. Exact-owner
filtering uses the key prefix before its final `::` segment. Call ownership is
the callsite prefix before `::call:`; dependency ownership is the record's exact
`owner`. No descendant-owned evidence is folded into an entity or direct file
owner array. The same rules govern file payload projections.

The file payload is:

```json
{
  "schema": "lockstep.architecture-file-semantics/v1",
  "identity": "path::@file", "file_sha256": "…",
  "definitions": [{"identity": "…", "semantic_dependency_sha256": "…"}],
  "imports": [{"identity": "…", "import_semantic_sha256": "…"}],
  "aliases": [{"binding": "…", "target": "…"}],
  "receivers": [{"binding": "…", "target": "…"}],
  "calls": [{"callsite": "…", "target": "…"}],
  "dependencies": [{
    "reference": "…", "owner": "…", "kind": "decorator|base|metaclass",
    "target": "…"
  }],
  "propagated_domains": [], "propagated_transitions": [],
  "propagated_lifecycle_clusters": [],
  "rule_inputs": {
    "allowlist_digest": "…", "primitive_digest": "…",
    "lifecycle_digest": "…", "schema_digest": "…",
    "threshold_digest": "…", "analyzer_version": "…",
    "rule_version": "…"
  }
}
```

Definitions are every file entity in full AST preorder. File imports preserve
import order. Alias/receiver/call/dependency arrays contain only direct
file-owner evidence; definition-owned evidence is bound by definition digests.

The one-hop payload is:

```json
{
  "schema": "lockstep.architecture-one-hop-semantics/v1",
  "identity": "root::@one_hop", "root": "stable root identity",
  "members": [{"identity": "…", "semantic_dependency_sha256": "…"}],
  "propagated_domains": [], "propagated_transitions": [],
  "propagated_lifecycle_clusters": [],
  "rule_inputs": {
    "allowlist_digest": "…", "primitive_digest": "…",
    "lifecycle_digest": "…", "schema_digest": "…",
    "threshold_digest": "…", "analyzer_version": "…",
    "rule_version": "…"
  }
}
```

The member pairs preserve the validated input order. Each semantic dependency
digest is SHA-256 of the canonical bytes of its exact closed payload.

Before any partial result, Task 4 rejects an unresolved call or dependency,
source-population mismatch among `SourceIndex`, `ResolutionIndex`, and the
primitive envelope, malformed/stale rule evidence, or ambiguous lifecycle
match. Graph vertices are every indexed entity plus each file owner; propagation
edges are only resolved internal owner→callee calls. Decorator/base/metaclass
dependencies bind digests but are not propagation edges. File propagated sets
union the file-owner vertex and all contained entities. Lifecycle clusters are
derived only from matched transition IDs.

Direct-row placement is exact: an indexed entity binding labels that entity;
an external entity binding labels each owner having an exact resolved invocation
of that target; and a callsite binding labels only the exact callsite owner.
Primitive and lifecycle matching remain independent. External calls and
decorator/base/metaclass dependencies never become SCC propagation edges.

### 4.6 Frozen per-kind metrics and candidates

Every diagnostic and manifest `baseline_metrics` is a closed kind-specific
object validated by checked-in
`engine/tests/architecture/architecture_metrics.schema.json`, canonical JSON
Schema draft 2020-12 with schema ID `lockstep.architecture-metrics/v1` and four
closed `$defs`: `function`, `one_hop`, `class`, and `file`. Every object has
`additionalProperties:false`; every listed key is required. Integer metrics are
JSON integers with minimum zero. Identity/digest strings use their exact stable
identity or 64-lowercase-hex patterns. Every set-valued array is unique and
ordered by the frozen domain, transition, cluster, trigger, or lexical identity
order rather than input discovery order.

The domain enum/order is the ten-item list in §4.4. Transition enum/order is
the transition-table row order in §4.4; lifecycle-cluster enum/order is first
appearance in that table. `unresolved_callsites` is an ordered unique array of
stable callsite identity strings, not a count. `signals` is a closed object of
the exact boolean keys below. `composite_score` is integer 0–6 and equals the
count of true signal values. `hard_triggers` is an ordered unique array limited
to the exact per-kind enum/order below. `candidate` is boolean and must equal
the recomputed rule; stored values never override analysis. The schema file is
serialized as UTF-8 canonical JSON with sorted object keys, compact separators,
and no trailing newline; `schema_digest` is its SHA-256 and appears in every
report, semantic dependency digest, and exception manifest.

Each `$defs` object also carries normative closed arrays
`x-lockstep-signal-order` and `x-lockstep-hard-trigger-order`, plus exact
`x-lockstep-candidate-rule`; the verifier compares them byte-for-byte with the
orders/rules below rather than treating them as optional annotations. Domain,
transition, and lifecycle-cluster enums appear as ordered schema arrays. Thus
the schema digest binds field types, signal/trigger order, component fields,
transition vocabulary, and candidate rule together.

Every named count/complexity/nesting/fan-out/component/helper score is a
nonnegative integer. Every domain/transition/cluster field is an ordered unique
array of its enum. Function `unresolved_callsites`, one-hop `members`, and class
`bases` are ordered unique stable-identity arrays; one-hop `root` is a stable
function identity. Class `mutable_fields` is an ordered unique array of exact
`self.` plus Python-identifier strings. File `subsystem_imports` is an ordered
unique array of normalized subsystem-label strings derived from import records,
sorted lexicographically. `signals` values and `candidate` are JSON booleans.
`hard_triggers` is an ordered unique string-enum array. These are the only value
types; null, number-as-string, float, and coercion are rejected.

For every kind, `hard_triggers` contains exactly every crossed hard threshold in
its enum order, and any nonempty hard-trigger array makes `candidate=true`.
Without a hard trigger, function/one-hop is a candidate exactly when at least
three signals are true and either `domain_mixing` or `lifecycle_mixing` is true.
Class/file requires at least three true signals and at least one of domain
mixing, lifecycle mixing, or respectively `cohesion_components` /
`definition_dependency_components` fragmentation. No prose heuristic or review
verdict changes the computed boolean.

**Function schema:** `cyclomatic`, `cognitive`, `max_nesting`,
`legacy_syntactic_fanout`, `resolved_fanout`, ordered `direct_domains`,
`propagated_domains`, `direct_transitions`, `propagated_transitions`,
`propagated_lifecycle_clusters`,
`unresolved_callsites`, six boolean `signals`, `composite_score`, ordered
`hard_triggers`, and `candidate`.

Function signal keys/order are `cyclomatic`, `cognitive`, `nesting`,
`legacy_syntactic_fanout`, `domain_mixing`, `lifecycle_mixing`. Function hard
trigger enum/order is `cyclomatic_gt_15`, `cognitive_gt_25`, `nesting_gt_4`,
`legacy_syntactic_fanout_gt_24`.

Function signals are cyclomatic ≥10, cognitive ≥15, nesting ≥4,
legacy syntactic fan-out ≥16, propagated domains ≥2, and propagated lifecycle
clusters ≥2. Hard adjudication triggers are cyclomatic >15, cognitive >25,
nesting >4, or legacy syntactic fan-out >24. A function is a candidate on any
hard trigger, or when at least three signals plus domain/lifecycle mixing hold.
Hard means mandatory adjudication, not automatic removal: a current reviewed
exception may pass.

**One-hop schema:** `root`, ordered `members`, `helper_count`,
`summed_cyclomatic`, `summed_cognitive`, `max_nesting`,
`legacy_syntactic_fanout_union`, `resolved_fanout_union`, propagated domain and
lifecycle unions as exact `propagated_domains`, `propagated_transitions`, and
`propagated_lifecycle_clusters`, six `signals`, `composite_score`,
`hard_triggers`, and `candidate`.

One-hop signal keys/order are `summed_cyclomatic`,
`summed_cognitive`, `nesting`, `legacy_syntactic_fanout_union`,
`domain_mixing`, `lifecycle_mixing`. Its sole hard trigger is
`helper_count_gt_12`.

For each root, begin with directly called same-file or same-class helpers whose
final name starts `_`, including name-mangled `__private`; exclude only true
dunders matching `^__.*__$`. Include a touched helper SCC as a whole, then
repeatedly remove a helper SCC with a production caller outside the current
root/closure until fixed point. Overlapping closures are measured for every
root. `members[0]` is exactly `root`; remaining helpers are sorted by stable
identity, and `helper_count == len(members) - 1`. Signals are summed cyclomatic
≥24, summed cognitive ≥40,
nesting ≥4, legacy syntactic fan-out union ≥32, domains ≥3, lifecycle clusters
≥2. Without a hard trigger, candidate requires at least three signals plus
domain or lifecycle mixing. More than 12 helpers is an independent hard
adjudication trigger for excessive reach.

**Class schema:** `method_count`, `public_method_count`, ordered
`mutable_fields`, `mutable_field_count`, `cohesion_components`, ordered direct
`bases`, `propagated_domains`, `propagated_transitions`,
`propagated_lifecycle_clusters`, six `signals`, `composite_score`,
`hard_triggers`, and `candidate`.

Class signal keys/order are `method_count`, `public_method_count`,
`mutable_field_count`, `cohesion_components`, `domain_mixing`,
`lifecycle_mixing`. Class hard-trigger enum/order is `method_count_gt_24`,
`mutable_field_count_gt_24`.

A public method does not start `_`. Mutable fields are unique `self.attr`
targets in Store/Del/AugAssign/AnnAssign, plus exact mutator calls from the
versioned mutator table on `self.attr` or on a single immutable local alias of
that attribute. Version 1 mutators are exactly `append`, `extend`, `insert`,
`remove`, `pop`, `clear`, `sort`, `reverse`, `update`, `setdefault`, `add`,
`discard`, `difference_update`, `intersection_update`, and
`symmetric_difference_update`. Dynamic `setattr/delattr`, unresolved aliases,
unknown mutators, and receiver escapes are unresolved analysis, not ignored.
The undirected class-cohesion graph has one vertex per direct lexical method and
each class-attributed lambda evidence vertex from §4.2. It connects vertices
that share a mutable/read `self`/`cls` field or have a resolved intra-class call;
lambda vertices affect only cohesion evidence/components, never method counts.
Decorators and inherited methods are semantic dependencies but not cohesion
vertices.
Signals are methods ≥15, public methods ≥8, mutable fields ≥8, cohesion
components ≥3, domains ≥3,
lifecycle clusters ≥2. Without a hard trigger, at least three signals plus
domain/lifecycle mixing or cohesion fragmentation makes a candidate. More than
24 methods or more than 24 mutable fields is an independent hard adjudication
trigger.

**File schema:** `definition_count`, `class_count`,
ordered `subsystem_imports`, `subsystem_import_count`,
`definition_dependency_components`, `propagated_domains`,
`propagated_transitions`, `propagated_lifecycle_clusters`, six `signals`,
`composite_score`, `hard_triggers`, and `candidate`.

File signal keys/order are `definition_count`, `class_count`,
`subsystem_import_count`, `definition_dependency_components`, `domain_mixing`,
`lifecycle_mixing`. File hard-trigger enum/order is `definition_count_gt_50`.

Definition count includes every lexical function/class. The component graph has
one vertex per top-level definition; references/calls made by a contained method
or nested definition are attributed to its top-level owner, and edges connect
distinct owners. Containment itself is recorded but is not a cohesion edge.
Imports, decorators, bases, and resolved aliases contribute semantic dependency
edges. A subsystem is the first segment after `lockstep`, or the exact top-level
module name. Signals are definitions ≥25, classes ≥6, subsystems ≥4,
domains ≥4, lifecycle clusters ≥3, dependency components ≥4. Without a hard
trigger, at least three signals plus domain/lifecycle mixing or dependency
fragmentation makes a candidate. More than 50 definitions is an independent
hard adjudication trigger.

### 4.7 Closed exception manifest and review evidence

The manifest is
`engine/tests/architecture/architecture_exceptions.json`. Top-level keys are
exactly `schema_version`, `ratchet_version`, `reference_commit`, `scan_root`,
`population`, `analyzer_digest`, `primitive_digest`, `allowlist_digest`,
`lifecycle_digest`, `schema_digest`, `threshold_digest`, and ordered
`exceptions`.

Each exception has exactly:

- `entity`, `kind`, ordered `trigger_reasons`;
- one concrete `responsibility` and one protected `invariant`;
- nonempty existing pytest node IDs in `focused_gate`;
- the exact per-kind `baseline_metrics` object;
- `source_sha256`, `semantic_dependency_sha256`, and
  `member_closure_sha256`;
- closed `review_evidence`;
- `next_review_gate` and exact `expires_on`.

`kind` is exactly `function`, `one_hop`, `class`, or `file` and selects the
matching schema definition. `trigger_reasons` is nonempty and recomputed in
this stable order: each crossed hard trigger as `hard:` plus its enum value,
then, only when the composite rule independently makes the entity a candidate,
each true signal as `signal:` plus its key in that kind's signal order. No
free-text, reviewer ordering, or omitted crossed reason is accepted.

`member_closure_sha256` hashes the ordered framed identity+semantic-digest list.
For `function` it is the root alone. For `one_hop` it is the metrics `members`
array: root first, then helper identities sorted as above. For `class` it is the
class root, direct lexical function/async/class children in AST order, then
uniquely resolved direct base identities in declared order, de-duplicated at
first occurrence. For `file` it is the file aggregate root, every contained
named definition in full AST preorder, then every import identity in
import-ordinal order; the root uses the file aggregate semantic digest, import
entries use their `import_semantic_sha256`, and all other entries use entity
semantic digests.
Framing is ASCII
`lockstep.architecture-members/v1\0`, followed by UTF-8 identity, NUL,
lowercase semantic digest, NUL. No line-ending normalization occurs.

`review_evidence` is a closed object with exactly:
`project_relative_artifact_path`, `git_tree_artifact_path`, `review_commit`,
`artifact_blob_sha256`, `reviewer_role`, `verdict`, `finding_counts`,
`reviewed_semantic_dependency_sha256`, and `review_evidence_sha256`.

`project_relative_artifact_path` is the inner-project POSIX path used for
project-local display/access, for example
`.superpowers/reviews/2026-08-29-task-12a5-proportionality.md`. It must be
nonempty UTF-8 of at most 4,096 bytes, contain no NUL/backslash, equal its
`PurePosixPath` serialization, be relative, contain no empty/`.`/`..` part, and
have at least one filename part after the exact `.superpowers/reviews/` prefix.
`git_tree_artifact_path` is exactly the literal POSIX prefix `lockstep/` plus
`project_relative_artifact_path`; it is the sole path used for committed-tree
lookup and blob access. The verifier rejects absolute paths, alternate inner
project prefixes, normalization differences, symlinks/submodules, and any
mismatch between the two fields.

`review_commit` is a 40-char lowercase hexadecimal ancestor commit.
`artifact_blob_sha256` is the lowercase SHA-256 of the artifact bytes at that
commit. `reviewer_role` is exactly `architecture`; `verdict` exactly `PASS`;
`finding_counts` exactly `{"critical":0,"important":0,"minor":0}`; and
`reviewed_semantic_dependency_sha256` is the entity's matching lowercase
digest. `review_evidence_sha256` hashes UTF-8 canonical JSON of all eight other
fields with sorted keys, compact separators, and no trailing newline, thereby
binding both path namespaces, commit, blob, role, verdict, counts, and entity
digest.

The verifier requires the `git_tree_artifact_path` lookup in the
`review_commit` tree to return exactly one regular blob, then reads its bytes
exactly as `git show review_commit:git_tree_artifact_path`, never through the
project-relative checkout path. It hashes those bytes and requires
`artifact_blob_sha256` equality. Project-local rendering or a human link uses
only `project_relative_artifact_path` resolved beneath the inner `lockstep/`
project root. It parses production, analyzer, schema, and rule-data bytes from
that same `review_commit` tree,
then the current verifier applies the frozen algorithm to those historical
bytes without executing historical Python. It recomputes the entity
source/semantic/member digests there and requires the reviewed digest and
artifact-named entity/digest to match that recomputation.
The commit must be an ancestor, role exactly `architecture`, verdict/counts
exact, and every focused node ID must collect in the current tree. Missing
historical bytes/rules, checkout substitution, placeholder paths, free text,
URLs, agent IDs, or self-attestation do not satisfy review evidence.

`expires_on` has exactly these all-true keys: `source_changed`,
`semantic_dependency_changed`, `member_closure_changed`, `any_metric_increased`,
`any_component_increased`, `composite_score_increased`, `new_domain`,
`new_lifecycle_cluster`, `focused_gate_missing_or_renamed`,
`review_evidence_unverifiable`, and `analyzer_or_rule_version_changed`.
Disappeared/noncandidate entities make entries stale and require deletion.
Initial `next_review_gate` is the final Task 12 source review; only fresh
independent review may move it to post-Task-12 roadmap reevaluation.

The ratchet passes only with zero unresolved callsites, every candidate
remediated or validly excepted, no worsened/expired/stale/duplicate entry, and
no mixed-responsibility candidate hidden by an exception. An exception is for a
cohesive outlier whose invariant makes decomposition unsafe in this range, not
a waiver for confirmed mixed responsibility.

### 4.8 Feasibility checkpoint and remediation order

Phase 1 begins tests-only: characterize legacy metrics, make the corrected
analyzer GREEN, and produce the full deterministic current inventory. Before
the first production edit, a mandatory feasibility report records every
function/one-hop/class/file candidate, its adjudication, focused gates, and for
each remediation wave the concrete files/entities, responsibility boundary,
preserved invariants, dependency order, and threat-model impact. The earlier draft review observed 54
candidates under incomplete rules; that number is evidence of risk, not a
frozen inventory.

If the complete remediation cannot preserve a named invariant, violates the
threat model or ownership model, or requires one of the prohibited abstractions
from §3, stop for a user design decision before any production edit. Do not
start an “easy” wave first.

For a feasible inventory, the remediation graph has edge owner→dependency.
First build resolved production-call edges, collapse SCCs, and define a leaf as
an SCC with no outgoing edge to another remaining candidate SCC. Process leaves
first. Second, add file owner→imported dependency edges among equal call levels,
collapse again, and process import leaves first. Stable tie order is path then
qualified identity. Pure validation/projection precedes stateful owners at an
otherwise equal leaf.

Every wave preserves public signatures, tested exceptions/diagnostics, exact
record bytes, transaction/lease/CAS/currentness/lock order, rename/fsync traces,
authority commitment, projection, and per-file authoring behavior. Each wave
gets focused behavior gates, repository ratchet, affected subsystem gate, and
independent architecture plus reliability review before its callers change.

## 5. Phase 2 — artifact export and real built-in templates

### 5.1 Honest exported-artifact grammar

In v1, `step.artifact` means **exported artifact** and therefore requires a
handle. There is no local-only artifact grammar.

```yaml
artifact:
  handle: review
  path: review.md
  markdown:
    sections: [Findings, Verdict]
```

The mapping has exactly `handle`, `path`, and `markdown`; `markdown` has exactly
`sections`. Handle is a unique logical identifier. Path is one safe exact
project-relative file covered by the same step's `writes`. Sections is an
ordered nonempty unique list of bounded nonempty headings. Duplicate
handle/path/producer, unsafe or uncovered paths, mismatched call mapping, and
destination overlap fail compilation before mutation.

`markdown.sections` is prompt and owner-review metadata only. The compiler
retains it in the generated interrupt message and compiler-owned managed brief.
The existing runtime descriptor contains only `name=handle`, `source_path=path`,
`media_type=text/markdown`, and `required=true`; runtime does not parse headings,
interpret a report's `PASS`/`FAIL` text, or enforce a Markdown verdict. The owner
reviews content before issuing a bearer. `accept.verdict: PASS` is the existing
engine acceptance outcome after exact consent; it is not a claim extracted from
artifact prose.

Semantic validation records child exports separately from parent-imported
artifacts. Child contracts derive exports only from that export collection.
Lowering binds the producer logical ID and its generated result state key to the
existing descriptor artifact. Exact exported source paths are removed from
`non_artifact_writes`; any remaining write makes call/parallel specialization
invalid. No runtime artifact schema, registry, protocol, state machine, or
lifecycle is added.

When specialization binds an export, it must not erase `artifact_contract` from
the non-authoritative interrupt message. The descriptor remains the sole
runtime contract; retained message metadata exists only for the managed brief
and human owner review.

### 5.2 Exact managed-step compiler/runtime projection

A standalone DSL `step` stays existing `kind=manual`. When a parent `call` with
`runner: codex` specializes a child manual step, the compiler performs this
exact projection:

1. create a collision-checked compiler state key and passthrough node before the
   effect interrupt; the node uses the existing stable compiler node identity
   with role `managed-brief`;
2. write the exact UTF-8 string
   `"Task:\n" + block.task + "\n\nExit criterion:\n" + block.exit + "\n"`
   to that key. For an exported artifact append
   `"\nArtifact path: " + artifact.path + "\nRequested Markdown headings: " +
   ", ".join(artifact.markdown.sections) + "\n"`;
3. redirect every original incoming edge to the brief node and add one
   unconditional brief-node→effect edge; retries therefore rebuild the same
   brief without changing attempt semantics;
4. change the descriptor to `kind=managed`, runner selector `codex`, and exact
   sorted capabilities `bounded_result`, `credentials`, `network`, `sandbox`,
   `workspace`;
5. derive the state key as `managed_brief_` plus the lowercase SHA-256 of ASCII
   `lockstep.managed-brief/v1`, NUL, the UTF-8 specialized namespace, NUL, and
   the UTF-8 logical ID;
   make descriptor input `brief` use the existing `StateSelector` for that
   derived key and descriptor input `snapshot` use the existing
   `RuntimeInputSelector` value `current_project_snapshot`;
6. retain logical ID, writes, descriptor artifacts, result schema, generated
   result key, scope keys, and call-scope deadline.

The compiler state key/node uses existing YAMLGraph transient state and adds no
durable schema or lifecycle. There is no new descriptor input named
`snapshot_ref`: the existing input name is `snapshot`. The existing
`RuntimeSnapshotResolver` selects `current_project_snapshot` from the exact
run-start root when no eligible predecessor successor exists, otherwise from
the greatest common ancestor of immutable successor snapshot refs whose native
coordinates precede the interrupt. It durably binds the selected
`EffectRuntimeInput.snapshot_ref` to the exact effect ID, public run, native
coordinate, descriptor digest, and runtime key, and returns the literal prefix
`snapshot:` followed by the lowercase snapshot SHA-256. `EffectCoordinator`
builds the immutable
request after owner grant resolution. `CodexRunnerAdapter.prepare` must receive
the exact brief and snapshot string, materialize that immutable snapshot, and
produce the existing prepared launch; the existing owner-current commitment
then gates `ensure_started`.

The Phase 2 RED uses public compile/provision/start with the production
`CodexRunnerAdapter` and a controlled owner-selected local executable. It uses
only non-secret deterministic test binding bytes and makes no network call. It
must reach the actual provider `prepare`, durable launch commitment, and
`ensure_started`—not a fake adapter or direct provider call—and prove exact
brief bytes, snapshot provenance, one spawn, rollover, result, artifact,
delivery, and recovery.

### 5.3 Exact `reviewed-change` product flow

The bundle remains parent plus one review child. Parent flow is exactly:

1. manual `plan`, requiring `.lockstep/plan.md` and headings Goal, Acceptance
   Criteria, Steps, with declared write `.lockstep/plan.md`;
2. manual `tests`, requiring acceptance tests before source implementation,
   with declared write prefix `tests/`;
3. manual `implement`, requiring implementation without weakening the frozen
   tests, with declared write prefix `src/`;
4. `verify` with exact command `pytest -q -p no:cacheprovider`, `cwd: .`, a
   900-second timeout, no writes, and selector `pinned` through existing
   lowering;
5. call ID `review`, workflow `{name}-review`, runner `codex`, mapping child
   handle `review` to `.lockstep/review.md`, with a five-minute timeout;
6. accept `review.review` to `.lockstep/review.md` with verdict `PASS` through
   exact bearer consent/publication.

The three manual steps and pinned verify each use retry limit 2 with exhaustion
to the existing escalation outcome. The child review call does not retry or
spawn a replacement reviewer implicitly; recovery adopts/reconciles its one
durable attempt.

The child contains one step ID `review`, writes only `review.md`, and exports
handle `review` with requested headings `Findings` and `Verdict`. Its task asks
for evidence-backed independent review of the plan, frozen tests,
implementation, and pinned verification, and says to write `PASS` only with no
blocking finding. Those words guide the managed reviewer; they are not consent.

The compiled inventory is exact: three manual effects, one `verify → pinned`
requirement, one specialized `managed → codex` requirement, and one acceptance
effect. Scope descriptors do not count as runtime requirements. Gate D
provisions exactly one credential-free pinned grant and one controlled Codex
grant satisfying the existing capability contract with non-secret fixture
binding bytes; it uses no real credential or network.

Acceptance evidence must close/reopen with the real child artifact and producer
lineage durable while accept is pending; recovery must adopt/reconcile without a
second spawn. The owner previews the exact commitment, issues a fresh bearer,
redeems it, writes the exact artifact bytes to `.lockstep/review.md`, records the
receipt/publication, resumes the accept, and reaches terminal state. Artifact
reference, blob digest/size, public run, project, definition digest, producer
effect, native coordinate, descriptor, destination, identity transformation,
local-project audience, consent epoch, and receipt remain hash/provenance-bound.
Automated RED/GREEN uses the production adapters with controlled owner-selected
executables and runs a real local sample pytest command. It exercises the real
process/lifecycle/artifact/bearer/recovery path deterministically without an
actual credential, external network, fake adapter, direct provider call, or
synthetic completion fact.

### 5.4 Exact `parallel-review` product flow

Security and architecture children each contain one managed-specializable step,
write only `security-review.md` or `architecture-review.md`, and export handle
`review` with Findings/Verdict prompt metadata. Parent `parallel` ID is
`reviews`, `join: all`, with branches/call IDs `security` and `architecture`,
both runner `codex`. Mappings are disjoint. Joined handles are exactly:

```text
reviews.security.security.review
reviews.architecture.architecture.review
```

The two provider executions must overlap in real time, use distinct immutable
workspaces/artifacts, survive partial/batch resume and close/reopen, join once,
and require separate exact bearers/publication receipts. Compiled runtime
inventory is exactly two managed Codex requirements and no pinned requirement.
The parallel scope and each call use five-minute timeouts; neither child retries
or spawns a replacement attempt implicitly.

Security prompt is limited to reachable boundaries in the frozen threat model
and asks for boundary, pre-existing authority, achieved authority, and delta.
Architecture prompt is limited to responsibility, dependency direction,
cohesion, and public-contract preservation. Report text never self-authorizes.

### 5.5 Authority delta and adversarial acceptance REDs

Template list/show/install/compile and configuration remain non-authoritative.
Only start after exact owner selection, grant, preflight, currentness, and
commitment can reach Local unsandboxed OS-user execution. Publication remains a
separate dynamic bearer boundary.

Phase 2 freezes these public-path adversarial REDs before GREEN:

- artifact text containing `PASS` without a bearer leaves accept pending with
  no consent, receipt, publish effect, publication journal, or destination write;
- an absent bearer has the same no-side-effect result;
- bearer/token from another commitment, stale/revoked consent epoch, wrong
  artifact ref or digest, wrong destination, changed definition digest, foreign
  run/project/coordinate/producer/descriptor, or changed transformation/audience
  is rejected before receipt/publication/resume;
- after every rejection, durable acceptance/publication facts and destination
  bytes remain unchanged;
- the exact fresh bearer authorizes only its one complete commitment, is
  idempotent only for that commitment, and cannot authorize another artifact or
  destination.

These use real registry provenance, owner consent, publication, native pending
acceptance, and restart. Synthetic result dictionaries do not satisfy the gate.

### 5.6 Per-file authoring reliability

The approved Task 12A contract remains exact:

- cooperating writers serialize on the existing project lock;
- the immutable transitive plan is validated before the first mutation;
- each file replacement is atomic and durable through its parent directory;
- a crash may leave old, new, or mixed generated files;
- there is no bundle transaction, rollback, journal, or automatic repair;
- repeated first install completes only a strict proper canonical prefix with
  exact planned bytes/modes; full, holed, mismatched, foreign, or noncanonical
  sets collide;
- explicit regeneration is the only repair;
- runtime start admits only a freshly observed complete canonical closure and
  exact DAG before durable runtime effects;
- legacy v4 evidence requires the pre-simplification recovery build and is
  never manually deleted.

Historical “atomic full-bundle install” assertions are retired, not restored.

## 6. Phase 3 — separate source, clean-wheel, and staged-plugin Gate D RED

Gate D starts only after Phases 1 and 2 are green, feasible, and independently
reviewed. It extends `engine/tests/test_plugin_packaging.py` and
`engine/tests/test_task12_plugin_identity.py`, creates
`engine/tests/test_installed_contract.py`, and changes tests only.

### 6.1 Source-checkout gate

From the tracked checkout, scan active production, README, `docs/DESIGN.md`,
skills, examples, manifests, and scripts for retired terms. Historical
`CHANGELOG.md` and
`docs/superpowers/specs/2026-08-19-codex-claude-parity-design.md` are the only
content exclusions and cannot be linked as current instructions. Run the exact
CLI/MCP example compiler. Exercise both templates through their full public
flows. Exercise a hand-written, marker-free manual YAMLGraph recipe through
check, estimate, start, observe/status/history/wait as applicable, explicit
recover after close/reopen, and terminal completion.

### 6.2 Clean-wheel gate

Build with `uv build`. Create a new temporary virtual environment outside the
checkout, unset `PYTHONPATH` and Python import-path variables, install the exact
wheel, change cwd to a fresh project outside the checkout, and assert every
`lockstep` module/resource path is inside that environment rather than the
checkout. Inspect wheel file list/content, run CLI/MCP/tool identity, package
resource list/show/init/compile, both real template inventories, the complete
reviewed artifact→restart→bearer→publication smoke, parallel overlap smoke, and
the same marker-free manual YAMLGraph check/estimate/start/observe/recover path.

### 6.3 Staged-plugin gate

Materialize a temporary plugin root containing only the existing tracked
delivery paths asserted by packaging tests—host manifests, `.mcp.json`, hooks,
skills, launcher/install scripts, engine lock/project files, source package, and
package resources. This is test-only copying, not a new production manifest or
packaging framework. Run launch/doctor/serve smoke from a foreign project cwd
with no checkout import. Assert host identity, active docs/skills, marker-free
manual YAMLGraph, both template full flows, artifact publication, bearer
acceptance, and close/reopen recovery use only staged bytes.

Across all three gates:

1. active bytes contain none of `_subcall`, `lockstep.subcalls`,
   `_subcall_wrapper.py`, `Subcalls (v2)`, active fractal/subcall prose,
   `runners.yaml`, `LOCKSTEP_RUNNER`, `RunnerSpec`, `load_runners`, legacy runner
   config, or `peak_parallel_subcalls`;
2. no legacy runner module/symbol/importer/build argv is shipped/importable;
3. every active example uses the exact current CLI/MCP grammar and compiles;
4. reviewed-change provisions exact one pinned plus one Codex grant; parallel
   provisions exact two Codex grants; configuration alone grants nothing;
5. smoke continues through real artifact materialization, pending acceptance,
   close/reopen recovery, exact bearer receipt, publication destination bytes,
   native resume, and terminal state;
6. estimate exposes `peak_parallel_child_calls`, schema v1, with no alias.

Every Task 12C source/wheel/staged-plugin scenario is deterministic automation
through production adapters and controlled owner-selected local executables.
The controlled executables supply bounded fixture results, use non-secret
binding material, and make/require no network call while still exercising actual
prepare/commit/start, overlap, result/rollover, artifact registration, bearer
acceptance, publication, restart, and recovery. Task 12C neither requires nor
permits an actual Codex account credential, external Codex/network call, or
operator-driven live scenario. This networkless fixture behavior is not a
confinement claim in the Local unsandboxed profile.

The focused Gate D command is:

```bash
cd engine
uv build
uv run pytest tests/test_plugin_packaging.py \
  tests/test_task12_plugin_identity.py tests/test_installed_contract.py \
  tests/workflow/test_estimate.py tests/test_recipe_cli.py -q
```

A checkout import, PYTHONPATH dependency, synthetic artifact/consent fact,
adapter bypass in the deterministic smoke, missing pinned verification, or early stop at
`start`/`observe` is a wrong-reason RED and returns to the owning phase.

## 7. Phase 4 — minimal installed-contract GREEN

GREEN performs only the frozen retirement:

- delete `engine/src/lockstep/runtime/runners.py` and
  `engine/tests/test_runners.py`;
- remove its production/public importers and symbols;
- rename estimate output to `peak_parallel_child_calls`, with no read/write
  alias and no schema-version bump;
- remove `LOCKSTEP_RUNNER` from manifests, launcher, active docs, skills, and
  tests;
- let `.mcp.json` set only `LOCKSTEP_PLUGIN_HOST=codex` so the launcher can
  derive `CODEX_HOME`; runtime never reads/interprets this non-authority marker;
- remove the runner environment entry from `.claude-plugin/plugin.json`;
- update active README/design/skills/examples/manifests/package assertions to
  this grammar, Local unsandboxed authority statement, manual YAMLGraph path,
  and real template flows;
- leave historical changelog/superseded parity spec unchanged and nonactive.

No shim, deprecated alias, migration framework, new selector, reusable ratchet
tool, packaging framework, or dependency addition is permitted.

## 8. TDD, dependency order, reviews, and completion evidence

The controlling execution sequence is:

1. tests-only ratchet/analyzer RED;
2. test-tooling GREEN with zero unresolved calls and full inventory;
3. semantic feasibility report and user stop if any invariant, ownership, or
   threat-model constraint fails;
4. if feasible, dependency-ordered production remediation waves, each reviewed;
5. tests-only artifact/managed/template RED including actual provider reach and
   adversarial bearer cases;
6. minimal artifact/template GREEN and authority review;
7. complete tests-only Gate D RED across source, clean wheel, staged plugin;
8. minimal installed GREEN;
9. affected gates, ratchet, full engine, compileall, build, clean-wheel,
   clean-installed/staged-plugin smoke, and `git diff --check`;
10. retain deterministic source/wheel/staged-plugin evidence for
    `reviewed-change`, overlapping `parallel-review`, and marker-free manual
    YAMLGraph: commands, snapshots, production-adapter observations, artifacts,
    consent receipts, publication bytes, recovery, and timings;
11. independent product/proportionality, architecture/SRP,
    behavior/reliability, threat-model, source, wheel, and installed/plugin
    reviews with Critical/Important/Minor zero;
12. stop and report exact evidence before any publication action.

Every behavior phase follows tests-only RED → causal RED verification →
independent RED C0/I0/M0 → minimal GREEN → focused and full verification →
independent GREEN C0/I0/M0. For the current dependency chain specifically:
the accepted contract-amendment review permits Task 3A RED; accepted Task 3A
RED review permits Task 3A GREEN; accepted Task 3A GREEN review permits Task 4
RED; and accepted Task 4 RED review permits Task 4 GREEN. Security findings
must name reachable boundary, pre-existing authority, achieved authority, and
delta. Review fixes repeat the narrowest causal cycle. Historical test counts
never substitute for fresh output.

## 9. Stop rules

Stop at the first:

- any RED before its controlling contract/amendment review, or any GREEN before
  that range's independent causal RED C0/I0/M0;
- wrong-reason RED, unexplained regression/change, or nonreproducible evidence;
- unresolved callsite, duplicate lexical identity, nondeterministic rule data,
  expired/stale exception, unreviewed candidate, or mixed responsibility hidden
  by exception;
- analyzer/test-tooling function violating its own hard gate;
- failed semantic feasibility checkpoint for a named invariant, ownership
  boundary, prohibited abstraction, or threat-model requirement;
- product flow missing manual plan/tests/implementation, pinned pytest,
  production-adapter Codex lifecycle, artifact, exact bearer publication, or
  recovery;
- managed Codex request missing exact brief/current snapshot/full capabilities,
  or not reaching production prepare/commit/start;
- any claim that Markdown headings/verdict are runtime-enforced;
- any `PASS` text/configuration/template/marker implying authority;
- acceptance/publication mutation without the exact current bearer commitment;
- checkout import/PYTHONPATH leakage in wheel/plugin evidence;
- rollback/journal/atomic-bundle/automatic-repair authoring behavior;
- old runner/subcall/environment/estimate surface in active installed bytes;
- missing independent zero-finding review;
- publication, push, issue/PR, merge, tag, release, version bump, marketplace
  change, Task 13, or post-Task-12 work without separate approval.

Return to the owning design boundary. Do not add a compatibility patch merely
to advance a gate.

## 10. Explicit exclusions

This active contract excludes:

- publication and every external repository/marketplace action;
- new CLI/MCP/template aliases, custom template paths, MCP template init;
- local-only artifact grammar, optional export handles, binary artifacts,
  directories/globs, arbitrary media types, or cross-project destinations;
- runtime Markdown/verdict parsing or automated owner-review substitution;
- new artifact/consent/publication schemas, protocols, state machines, owners,
  locks, providers, schedulers, or compatibility layers;
- constrained-runner/sandbox security claims in Local unsandboxed deployment;
- crash-atomic multi-file authoring, rollback, recovery journal, automatic
  mixed-output repair, or manual legacy deletion;
- reusable architecture package, public CLI, skill, generic plugin packaging
  framework, or post-Task-12 roadmap implementation;
- actual Codex credentials, external Codex/network calls, or operator-driven
  live reviewed-change/parallel-review/manual acceptance in Task 12C; those
  scenarios remain Task 13-only candidates after the mandatory post-Task-12
  roadmap reevaluation and separate user approval;
- hardening against an actor already capable of arbitrary TCB execution when no
  untrusted reference crosses a reachable boundary.

## 11. Independent review response matrix

| Finding | Corrected sections | Resolution |
| --- | --- | --- |
| Product I1 | Status; §1; §8–§10 | Active controlling contract; independent zero-finding review gates each implementation range without intermediate user approval |
| Product I2 | §2; §5.3; §6 | Restored manual plan/tests/implementation, pinned pytest, production-adapter Codex lifecycle, artifact, accept/restart; exact 1 pinned + 1 Codex inventory |
| Product I3 | §1; §5.2 | Exact compiler brief/current-snapshot/capability projection and actual Codex prepare/commit/start RED |
| Product I4 | §5.1; §5.3; §5.5 | Sections/verdict are prompt/owner-review metadata, never runtime enforcement; consent is separate |
| Product I5 | §6.1–§6.3 | Separated checkout, clean-wheel/no-PYTHONPATH, and staged-plugin gates; smoke completes artifact acceptance |
| Product I6 | §2; §6.1–§6.3 | Marker-free manual YAMLGraph is a first-class full acceptance path on every appropriate surface |
| Product I7 | §3; §4.8 | Semantic feasibility checkpoint governed by invariants, ownership, and threat model; no LOC caps |
| Product M1 | §1; §7 | Corrected legacy test path to `engine/tests/test_runners.py` |
| Architecture C1 | §4.3–§4.5 | Preserved legacy syntactic fan-out, stable resolution, set-valued primitives, exact lifecycle table/digests, SCC fixed-point propagation, zero unresolved sites |
| Architecture C2 | §4.2; §4.5–§4.7 | Duplicate identity rejection, decorator spans, semantic dependency/member digests, exact per-kind schemas and verifiable review evidence |
| Architecture I1 | §4.6 | Independent hard adjudication for giant classes/files rather than three-signal escape |
| Architecture I2 | §4.6 | `__private` inclusion except true dunders and >12-helper hard reach trigger |
| Architecture I3 | §4.2–§4.6 | Closed containment/mutation/alias/decorator/inheritance/nested/receiver and class/file graph rules |
| Architecture I4 | §3; §4.8 | Full inventory and per-wave responsibility/invariant/threat-model feasibility before production edit |
| Architecture I5 | §4.1; §10 | Seven internal owners, analyzer self-gate, no reusable package/CLI/skill now, roadmap-only reevaluation |
| Architecture I6 | Status; §1; §8–§10 | Bound implementation authority to the active contract and its independent review boundaries |
| Architecture plan note | §4.8 | Defined owner→dependency edge, leaf SCC, call pass, then imports pass |
| Threat I1 | Status; §1; §9 | Bound threat-sensitive implementation to the active contract and independent zero-finding review boundaries |
| Threat I2 | §5.1; §5.5 | Export handle required; no local-only ambiguity; honest Markdown semantics and bearer separation |
| Threat I3 | §2; §5.2; §7 | Explicit Local unsandboxed single-user profile, ambient OS-user TCB, no confinement claim |
| Threat I4 | §5.3; §5.5; §6 | Exact provenance/hash commitment and full negative bearer/publication/restart gates |
| Threat I5 | §5.3; §6 | Reviewed-change includes and provisions the exact pinned verification requirement |
| A-C1 | §4.4–§4.5 | Separate coexisting effect-domain/lifecycle rows; stable entity or exact callsite literal discriminant; generic ambiguity gets no transition; canonical table digest frozen |
| A-C2 | §4.2; §4.5–§4.7 | Import identity/digest, exact schema/types/enums/orders/digest, closure order/meaning, committed review blob digest, and same-tree recomputation frozen |
| A-I3 | §4.2–§4.3 | Lambdas conservatively attributed; Python lexical Store/Del/global/nonlocal/conditional/shadow rules and exact annotated-parameter→self-field DI closed |
| A-I5 | §4.1 | Seven one-role modules with one-direction imports, forbidden ownership combination, and complete function/one-hop/class/file zero-exception self-gate |
| Product N-I1 | §2; §5.2–§5.4; §6; §8–§10 | Task 12C is deterministic production-adapter automation without real credentials/network; live scenarios reserved to post-reevaluation, separately approved Task 13 |
| Round 3 A-C2 | §4.7 | Closed review evidence distinguishes validated inner-project and `lockstep/` Git-tree paths, binds both in its digest, and uses each only in its owning namespace |
| Round 3 A-I3 | §4.2 | Exact lambda owner order is function→class→file; class-body lambda evidence enters semantic/unions/mutation/cohesion without becoming an indexed entity |

After the current Task 3A/Task 4 amendment reaches independent C0/I0/M0, the
next permitted action is Task 3A tests-only RED. No intermediate user approval
is required. This authority does not extend to Task 13, publication, or any
other separately prohibited external action.
