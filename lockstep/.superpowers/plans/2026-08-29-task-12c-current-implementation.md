# Task 12C Current Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Task 12C with a repository-wide architecture ratchet and feasible remediation, honest DSL artifact export, production-adapter `reviewed-change` and `parallel-review`, and a clean installed contract with all legacy runner/subcall surface retired.

**Architecture:** Work in four gated ranges. First build the seven-role test-owned analyzer and stop on its complete inventory before any production remediation. Then implement only inventory-proven remediation and the frozen DSL/template projection, freeze the full installed-contract RED, and finish with the smallest retirement GREEN. Existing runtime authority, artifact, consent, publication, snapshot, and recovery owners remain unchanged.

**Tech Stack:** Python 3.11+, pytest, `uv run --no-sync`, AST, canonical JSON/SHA-256, YAMLGraph 0.5.22, LangGraph 1.2.10, wheel/staged-plugin black-box tests.

**Spec:** `.superpowers/specs/2026-08-29-task-12c-current-design.md`

## Global Constraints

- Work only in `<HOME>/Projects/pets/claude-essentials-worktrees/lockstep-workflow-dsl` on `feat/lockstep-workflow-dsl`; do not delete or recreate the worktree. The execution/branch start is `7468b9b49ecefb45462bec2789877c73b0243f6a`; `4674e43fa1ffef1b9013f29345b2c7934808131e` remains the reference baseline for behavioral and inventory comparisons only.
- Preserve the final Task 12C product goal. Do not turn architecture remediation or review findings into unrelated cleanup.
- Add zero durable schemas, state machines, lifecycle owners, lock families, provider abstractions, schedulers, compatibility aliases, dependency patches, or reusable analyzer surfaces.
- Do not collect or use physical line counts, patch-size measurements, gross/net LOC, analyzer-size caps, or per-phase LOC caps as requirements, quality gates, review signals, or stop conditions. Judge proportionality through responsibility cohesion, threat-model fidelity, named invariants, complete tests, and non-line-based god-object metrics.
- Keep manual YAMLGraph first-class and marker-free.
- Use credentialless, networkless controlled local executables through production adapters. Actual credentialed/networked Codex scenarios belong only to separately approved Task 13 after the post-Task-12 roadmap reevaluation.
- Use `uv run --no-sync pytest`. Review every tests-only RED and minimal GREEN independently.
- Stop before production remediation if the full inventory requires a new durable schema/state machine/lifecycle owner/lock family, loses a named invariant, violates the ownership model, or changes the threat model without explicit approval.
- Stop before publication, push, issue/PR, merge, tag, release, version bump, marketplace change, Task 13, or post-Task-12 work.

---

### Task 1: Freeze Baseline and Analyzer Contracts

**Files:**
- Modify: `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/progress.md`
- Create: `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/task-12c-baseline.md`
- Modify: `engine/tests/architecture/test_no_god_methods.py`
- Create: the seven role modules `architecture_source_index.py`, `architecture_legacy_metrics.py`, `architecture_call_resolver.py`, `architecture_domain_lifecycle.py`, `architecture_candidate_policy.py`, `architecture_manifest_verifier.py`, and `architecture_diagnostics.py` as interface-only skeletons after the ownership RED review.

**Interfaces:**
- Consumes: Git baseline `7468b9b49ecefb45462bec2789877c73b0243f6a`.
- Produces: reproducible tracked-population evidence and a tests-only collection gate naming all seven analyzer modules.

- [ ] **Step 1: Record the clean baseline**

Run:

```bash
git status --short --branch
git rev-parse HEAD
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
uv run --no-sync pytest -q
```

Expected: clean `feat/lockstep-workflow-dsl` at `7468b9b...`; preserve exact pass/skip counts. Any historical physical-length warning is obsolete and must be removed before inventory acceptance.

- [ ] **Step 2: Record reproducible production and test populations**

Run a checked-in-independent measurement from `lockstep/engine`:

```bash
git ls-files 'src/lockstep/**/*.py' 'src/lockstep/*.py' | sort
git ls-files 'tests/**/*.py' 'tests/*.py' | sort
```

Write the commands, tracked file/entity counts, and interpretation to
`task-12c-baseline.md`. Do not collect physical line counts.

- [ ] **Step 3: Add the tests-only analyzer ownership RED**

Make `test_no_god_methods.py` import exactly:

```python
from architecture_candidate_policy import evaluate_candidates
from architecture_call_resolver import resolve_calls
from architecture_diagnostics import render_report
from architecture_domain_lifecycle import propagate_semantics
from architecture_legacy_metrics import measure_legacy_metrics
from architecture_manifest_verifier import verify_manifest
from architecture_source_index import build_source_index
```

Add a test asserting those seven modules are the complete role set and their allowed internal import edges equal the spec's directed table.

- [ ] **Step 4: Verify the RED fails for missing analyzer roles**

Run:

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
```

Expected: collection failure naming the first missing role module, not a production failure.

- [ ] **Step 5: Obtain independent RED review**

Reviewer brief: verify that the RED enforces seven one-role modules, repository-wide scope, test ownership, frozen import direction, and no production edit. Record C/I/M findings in `.superpowers/reviews/2026-08-30-task-12c-analyzer-red.md`; fix until C0/I0/M0.

- [ ] **Step 6: Create the minimal ownership GREEN**

Create all seven role modules with only the public entrypoints listed in Tasks
2–5. Every function raises `NotImplementedError`; no analyzer behavior, rule
data, or production code is added in this step.

- [ ] **Step 7: Verify collection and import-direction GREEN**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'role_modules or import_direction' -q
```

Expected: the ownership/collection contract passes. Behavior tests do not yet
exist, so no later analyzer role can mask a focused RED with a collection error.

- [ ] **Step 8: Obtain independent ownership GREEN review**

Require C0/I0/M0 on the seven public interfaces and one-direction imports
before adding source-index behavior.

---

### Task 2: Build Source Index and Preserve Legacy Metrics

**Files:**
- Modify: `engine/tests/architecture/architecture_source_index.py`
- Modify: `engine/tests/architecture/architecture_legacy_metrics.py`
- Modify: `engine/tests/architecture/test_no_god_methods.py`

**Interfaces:**
- Produces: immutable `SourceIndex`, `Entity`, `ImportRecord`, `SourceSpan`, and `LegacyMetrics`.
- `build_source_index(repo_root: Path, tracked_paths: Sequence[str], files: Mapping[str, bytes] | None = None) -> SourceIndex`.
- `measure_legacy_metrics(index: SourceIndex) -> Mapping[str, LegacyMetrics]`.

- [ ] **Step 1: Add source-index contract tests**

Cover every tracked `*.py` below `engine/src/lockstep`, decorator-inclusive spans, nested named definitions, duplicate stable identities, AST-order imports, CRLF-preserving digests, function→class→file lambda attribution, class-lambda `@lambda:NNNN` evidence, and >9,999 import/call ordinal failure.

In the same tests-only edit, add legacy characterization fixtures proving the
current cyclomatic, cognitive, nesting, and pruned syntactic-fanout
results, including the rule that nested function/class/lambda bodies do not
inflate their lexical parent's frozen metrics.

- [ ] **Step 2: Run the source-index RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'source_index or identity or span or lambda or import or legacy' -q
```

Expected: failures for absent index records, digest rules, and legacy metric
implementation.

- [ ] **Step 3: Obtain independent source-index/legacy RED review**

Require C0/I0/M0 on the failing contract and correct failure reasons before
implementing either role.

- [ ] **Step 4: Implement the minimal immutable source index**

Use stable identities:

```python
entity_id = f"{relative_posix_path}::{lexical_qualified_name}"
import_id = f"{relative_posix_path}::import:{ordinal:04d}"
```

Hash exact bytes and canonical JSON with sorted keys, compact separators, UTF-8, and no trailing newline. Do not interpret effect domains or candidates in this module.

- [ ] **Step 5: Implement the already-reviewed legacy metrics contract**

Move the current cyclomatic, cognitive, nesting, and pruned syntactic-
fanout algorithms byte-for-behavior into
`architecture_legacy_metrics.py`. Do not add or change tests after the RED
review unless a review finding restarts the RED cycle.

- [ ] **Step 6: Run focused GREEN and self-gate**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'source_index or legacy' -q
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
```

- [ ] **Step 7: Obtain independent GREEN review**

Require C0/I0/M0 on identity stability, byte-exact hashing, containment, lambda ownership, legacy parity, and one-role ownership.

---

### Task 3: Resolve Calls with Closed Python Binding Rules

**Files:**
- Modify: `engine/tests/architecture/architecture_call_resolver.py`
- Create: `engine/tests/architecture/architecture_effect_free_allowlist.json`
- Create: `engine/tests/architecture/architecture_effect_primitives.json`
- Modify: `engine/tests/architecture/test_no_god_methods.py`

**Interfaces:**
- Consumes: `SourceIndex`.
- Produces: `CallResolution` records keyed by `<owner>::call:NNNN`, normalized targets, immutable aliases/receivers/dependencies, and exact unresolved sites.
- `resolve_calls(index: SourceIndex, allowlist: object, primitives: object) -> ResolutionIndex`.

- [ ] **Step 1: Add resolver RED fixtures**

Cover lexical Store/Del/global/nonlocal behavior, declaration-after-use, conditional assignments, imports and aliases, `Class.method`, `self`/`cls`/`super`, immutable constructor receivers, inline receivers, class-wide fields, exact annotated-parameter injection, ambiguous inheritance, reflective/dynamic calls, callsite primitive rows, and stable ordinals.

- [ ] **Step 2: Verify the resolver RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'resolver or callsite or binding or receiver' -q
```

- [ ] **Step 3: Obtain independent resolver RED review**

Require C0/I0/M0 on the closed binding cases and correct RED reasons before
implementation.

- [ ] **Step 4: Implement closed resolution**

Return one of:

```python
ResolvedCall(callsite=callsite_id, target=normalized_target)
UnresolvedCall(callsite=callsite_id, line=line, column=column, ast_dump=dump)
```

Never classify by substring, fuzzy spelling, regex, or assumed purity. Keep table loading/validation outside source indexing.

- [ ] **Step 5: Populate exact rule data until the reference population has zero unresolved calls**

Each primitive row is exactly `selector_kind`, `selector`, `semantic_target`, and ordered unique `domains`. Entity and callsite selectors remain disjoint. Changes to source ordinal or semantic digest invalidate callsite rows.

- [ ] **Step 6: Run resolver GREEN and regression**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
```

Expected: zero unresolved reference-population callsites.

- [ ] **Step 7: Obtain independent resolver GREEN review**

Require C0/I0/M0 on Python lexical fidelity, conservative ambiguity, rule-table closure, stable callsites, and absence of effect policy inside the resolver.

---

### Task 3A: Add Task 4 Resolution Evidence Prerequisites

**Files:**
- Modify: `engine/tests/architecture/test_no_god_methods.py`
- Modify after accepted RED only: `engine/tests/architecture/architecture_call_resolver.py`

**Interfaces:**
- Preserve exact `ResolvedCall`/`UnresolvedCall` shapes and call semantics.
- Extend `ResolutionIndex` with exact source-population provenance and immutable typed literal evidence for every existing callsite.
- Accept exact same-identity indexed-entity primitive rows without assigning or propagating domains in the resolver.

- [ ] **Step 1: Add the tests-only prerequisite RED**

Freeze exact frozen/slotted `PositionalLiteralEvidence`,
`KeywordLiteralEvidence`, and `CallsiteEvidence` fields; exact six-field
`ResolutionIndex`; same-key/same-order deep immutability; file/entity/class/
lambda ownership; direct `null`/`bool`/`int`/`str` constants; booleans distinct
from integers; missing/spread/nonliteral/unsupported omission; exact source
population digest; mismatch rejection; and internal entity primitive
selector/semantic-target equality. Preserve all previously accepted resolver
contracts and canonical rule data.

This RED explicitly supersedes the earlier exact-four-field `ResolutionIndex`
shape assertion with the exact six-field shape from spec §4.3. It does not
change any accepted `ResolvedCall`, `UnresolvedCall`, alias, receiver,
dependency, resolution, or effect-coverage assertion.

- [ ] **Step 2: Verify and independently review the RED**

Require causal failures only for the superseded index shape, absent
`call_evidence`/provenance, and rejected internal entity row. Every prior
resolver selector other than the expressly superseded four-field-shape
assertion must remain green. Require C0/I0/M0 before implementation.

- [ ] **Step 3: Implement the minimal resolver evidence GREEN**

Build evidence from the resolver-owned `_Model` call ordering. Expose no AST,
perform no domain/lifecycle propagation, and do not change primitive JSON unless
the frozen reference population itself proves a required exact row.

- [ ] **Step 4: Verify and independently review the GREEN**

Run the prerequisite selector, all prior resolver/reference/canonical gates,
the full architecture suite, and the full project suite proportionately.
Require C0/I0/M0 before entering Task 4.

---

### Task 4: Propagate Domains and Lifecycle Through SCCs

**Files:**
- Modify: `engine/tests/architecture/architecture_domain_lifecycle.py`
- Create: `engine/tests/architecture/architecture_lifecycle.json`
- Modify: `engine/tests/architecture/test_no_god_methods.py`

**Interfaces:**
- `propagate_semantics(index: SourceIndex, resolutions: ResolutionIndex, primitives: object, lifecycle: object, *, digest_inputs: SemanticDigestInputs) -> SemanticIndex`.
- Produces separate direct/propagated domain sets, transition IDs, lifecycle clusters, and `semantic_dependency_sha256`.
- `SemanticIndex.build_one_hop(*, root: str, members: Sequence[str]) -> OneHopSemantics` only aggregates the exact Task 5-selected ordered members; it never selects or adjudicates membership and does not add a second top-level public entrypoint.

- [ ] **Step 1: Add SCC/lifecycle RED fixtures**

Cover recursive SCC union, caller propagation, set-valued domains, coexistence of domain and transition rows, entity invariants, literal-argument callsite discriminants, target mismatch, duplicate bindings, nonliteral arguments, import/decorator/base changes, and deterministic digest invalidation. Freeze the strict primitive/lifecycle schemas and single canonical serializer; exact dataclass field order/frozen/slotted/deep immutability; closed entity/file/one-hop payload and nested shapes; exact-owner binding projections and order; pre-partial source-population/unresolved-call/unresolved-dependency blocks; and exclusion of external calls and decorator/base/metaclass dependencies from SCC edges. Cover `SemanticIndex.build_one_hop` success plus rejection of empty, duplicate, unknown, mixed-file, root-not-first, and non-stable-identity-sorted member sequences; prove its tuples union only supplied members and it never discovers or adjudicates membership.

- [ ] **Step 2: Verify the SCC RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'domain or lifecycle or transition or scc or semantic_digest or one_hop' -q
```

- [ ] **Step 3: Obtain independent domain/lifecycle RED review**

Require C0/I0/M0 on propagation and digest failure reasons before
implementation.

- [ ] **Step 4: Implement the frozen transition vocabulary and fixed point**

Represent graph edges as owner→callee, collapse strongly connected components, union direct sets inside each SCC, then propagate callee sets to callers in reverse topological order. Keep domains, transition IDs, and clusters as separate ordered sets.

- [ ] **Step 5: Bind semantic dependency digests**

Hash identity, source, decorators/bases, imports, aliases, callsites/targets, direct and propagated sets, containment, schema/rule digests, and analyzer/rule version exactly as specified.

- [ ] **Step 6: Run focused GREEN**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
```

- [ ] **Step 7: Obtain independent domain/lifecycle GREEN review**

Require C0/I0/M0 on propagation direction, exact vocabulary, independent rows,
and digest completeness.

---

### Task 5: Candidate Policy, Manifest Verification, Diagnostics, and Self-Gate

**Files:**
- Modify: `engine/tests/architecture/architecture_candidate_policy.py`
- Modify: `engine/tests/architecture/architecture_manifest_verifier.py`
- Modify: `engine/tests/architecture/architecture_diagnostics.py`
- Create: `engine/tests/architecture/architecture_metrics.schema.json`
- Create: `engine/tests/architecture/architecture_thresholds.json`
- Create: `engine/tests/architecture/architecture_exceptions.json`
- Modify: `engine/tests/architecture/test_no_god_methods.py`

**Interfaces:**
- `evaluate_candidates(index, legacy, semantics, resolutions) -> ArchitectureReport`.
- `verify_manifest(report, manifest, *, repo_root: Path, current_commit: str) -> ManifestVerdict`.
- `render_report(report, verdict) -> str`.

- [ ] **Step 1: Add exact per-kind metric REDs**

Test function, one-hop, class, and file schemas; signal order; hard-trigger order; composite formulas; helper SCC closure; class cohesion/mutable fields; file dependency components; canonical schema digest; and malformed/stale/duplicate manifest rejection.

- [ ] **Step 2: Add historical review-evidence REDs**

Create a temporary Git history and prove the verifier uses `git_tree_artifact_path`, reads one regular blob from `review_commit`, hashes exact bytes, recomputes historical analysis without executing historical Python, validates ancestor/focused node IDs, and rejects checkout substitution or path normalization.

- [ ] **Step 3: Verify the candidate/manifest RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'candidate or one_hop or cohesion or manifest or review_evidence or diagnostics' -q
```

- [ ] **Step 4: Obtain independent candidate/manifest RED review**

Require C0/I0/M0 on schema closure, candidate formulas, historical evidence,
and correct RED reasons before implementation.

- [ ] **Step 5: Implement policy and manifest verification**

Candidate rules are data-bound, deterministic, and recomputed; stored `candidate`, `signals`, metrics, and reasons never override analysis. Exceptions may retain only cohesive outliers with a concrete responsibility/invariant and fresh C0/I0/M0 architecture evidence.

- [ ] **Step 6: Implement diagnostics as pure rendering**

Sort by normalized path, AST order, kind, and stable identity. Diagnostics accept computed immutable results and must not index, resolve, evaluate policy, or read the manifest.

- [ ] **Step 7: Enforce the analyzer's own zero-exception hard gate**

Run the complete function/one-hop/class/file policy over every analyzer support module. No analyzer function, aggregate, class, or file may receive an exception.

- [ ] **Step 8: Run analyzer conformance GREEN and semantic self-gate**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'not repository_ratchet' -q
```

Expected: analyzer fixtures, historical verification, zero unresolved
reference-population callsites, and analyzer self-gate pass without an
ownership, cohesion, or god-object exception.

- [ ] **Step 9: Obtain independent analyzer-conformance GREEN reviews**

Both reviews must reach C0/I0/M0 before inventory is treated as authoritative.

- [ ] **Step 10: Run the repository ratchet as the inventory RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k repository_ratchet -q
```

Expected: a deterministic failure listing every unremediated/unexcepted current
candidate. This is the repository RED and remains RED through Task 6.

- [ ] **Step 11: Obtain independent repository-ratchet RED review**

Require C0/I0/M0 on complete candidate emission and the correct failure reason
before using the output for feasibility.

---

### Task 6: Produce Complete Inventory and Enforce Semantic Feasibility

**Files:**
- Create: `.superpowers/reports/2026-08-30-task-12c-architecture-inventory.md`
- Modify: `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/progress.md`

**Interfaces:**
- Consumes: deterministic `ArchitectureReport`.
- Produces: every candidate's metrics, adjudication, focused gates, SCC/import dependency order, responsibility boundary, preserved invariants, and threat-model impact.

- [ ] **Step 1: Generate the complete inventory twice**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k repository_ratchet -q
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k repository_ratchet -q
```

Both commands are expected RED until Task 7. Require byte-identical ordered
diagnostics and report data.

- [ ] **Step 2: Adjudicate every candidate**

For each function, one-hop aggregate, class, and file, record exactly one of:
`remediate` or `cohesive-exception`. A mixed-responsibility candidate cannot use an exception.

- [ ] **Step 3: Build the remediation DAG**

Collapse candidate call SCCs; process leaves first. At equal call level add file owner→imported dependency edges, collapse again, and process import leaves first. Tie-break by path then qualified identity; prefer pure validation/projection over stateful owners.

- [ ] **Step 4: Demonstrate the complete Task 12C semantic path**

For every remediation wave list exact production files/entities, focused gates,
responsibility boundary, preserved invariants, dependency order, and
threat-model impact. Include the frozen semantic path for DSL export/managed
specialization/templates and installed-contract retirement. Reject needless
abstractions, duplicate ownership, unrelated cleanup, or weakened evidence;
do not estimate or gate the work by patch line counts.

- [ ] **Step 5: Apply the critical stop**

Stop and present facts/options to the user before any production edit if the
full remaining Task 12C path cannot preserve every named invariant, ownership
boundary, and threat-model requirement, or requires a prohibited abstraction.
Do not start an easy wave first and do not use an exception to hide mixed
responsibility.

- [ ] **Step 6: Obtain independent inventory review**

Require C0/I0/M0 on completeness, adjudication, wave ordering, semantic-path
feasibility, invariant preservation, ownership, threat-model fidelity, and
scope discipline. Production work remains blocked until this review and the
feasibility decision pass.

---

### Task 7: Remediate Only Inventory-Proven Mixed Responsibilities

**Files:**
- Modify: only production files named by the accepted inventory.
- Test: only focused behavior gates named by the accepted inventory.
- Modify: `engine/tests/architecture/architecture_exceptions.json` only for cohesive outliers with committed review evidence.
- Create: `.superpowers/reviews/2026-08-30-task-12c-architecture-exceptions.md` when at least one cohesive exception exists.

**Interfaces:**
- Preserves public signatures, exact generated/record bytes, transaction/lease/CAS/currentness/lock order, rename/fsync traces, authority commitment, projection, and per-file authoring behavior.

- [ ] **Step 1: Freeze focused behavior for the next leaf wave**

Add or select the smallest tests that prove the wave's named invariant. Run them before production edits.

- [ ] **Step 2: Make the focused test RED only when behavior is missing**

For pure decomposition with unchanged behavior, use the repository ratchet candidate as the RED and retain focused behavior GREEN throughout.

- [ ] **Step 3: Verify and independently review the wave RED**

When behavior is missing, run the exact focused node IDs from the accepted
inventory and require the expected assertion failure. When behavior is already
frozen, require focused GREEN plus the ratchet candidate for that wave as the
structural RED. Obtain an independent C0/I0/M0 RED review before any production
edit.

- [ ] **Step 4: Implement the minimum responsibility split**

Change only accepted inventory entities. Do not introduce registries, general frameworks, compatibility layers, or unrelated cleanup.

- [ ] **Step 5: Verify focused behavior and the remaining inventory**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k 'not repository_ratchet' -q
uv run --no-sync pytest -k 'not repository_ratchet' -q
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -k repository_ratchet -q
```

Before the full suite, run every exact focused pytest node ID recorded for the
wave in the accepted architecture inventory report. The report is produced and
independently verified by Task 6, so the command contains no guessed test name.
The focused nodes, analyzer conformance suite, and full regression excluding
`repository_ratchet` must be GREEN. Until the final wave, the isolated
repository-ratchet command is expected RED: its ordered candidate set must
equal the accepted remaining inventory exactly, with the completed wave
removed and no new, worsened, expired, or stale candidate. Only Step 11 runs
the unfiltered architecture/full suite expecting GREEN.

- [ ] **Step 6: Revalidate scope and invariants after every wave**

```bash
cd lockstep/engine
test -n "$(git ls-files 'src/lockstep/**/*.py' 'src/lockstep/*.py')"
git diff --name-status -M 4674e43fa1ffef1b9013f29345b2c7934808131e -- src/lockstep tests
```

Inspect every rename and changed path, confirm it belongs to the accepted wave,
and rerun its named invariant, ownership, threat-model, focused, ratchet, and
regression gates. The tracked-production sanity check must be nonempty. Stop
immediately on unexplained scope or invariant failure.

- [ ] **Step 7: Establish the exact reviewed tree**

Stage only the production, analyzer, schema, rule-data, manifest-independent
tests, and focused gates named by the accepted wave. Create a local GREEN
commit, then require no relevant unstaged or staged diff:

```bash
git diff --exit-code -- lockstep/engine/src/lockstep lockstep/engine/tests/architecture
git diff --cached --exit-code -- lockstep/engine/src/lockstep lockstep/engine/tests/architecture
```

This local commit is review evidence only; it does not authorize push or any
external action.

- [ ] **Step 8: Create verifiable evidence for each cohesive exception**

For every accepted cohesive candidate, run its focused gates. The independent
architecture reviewer writes one closed section in
`.superpowers/reviews/2026-08-30-task-12c-architecture-exceptions.md` with
the exact entity, `semantic_dependency_sha256`, responsibility, protected
invariant, focused node IDs, verdict PASS, and C0/I0/M0 counts. The verifier
requires the section matching each manifest entity/digest; one section cannot
attest another entity.

Force-add only that ignored artifact and create the local evidence commit:

```bash
git add -f -- lockstep/.superpowers/reviews/2026-08-30-task-12c-architecture-exceptions.md
git commit -m "docs(architecture): record Task 12C exception review"
```

Set `review_commit` to that artifact commit and prove it changes no reviewed
production/analyzer/schema/rule byte relative to its parent:

```bash
review_commit=$(git rev-parse HEAD)
git diff --exit-code "$review_commit^" "$review_commit" -- lockstep/engine/src/lockstep lockstep/engine/tests/architecture
git ls-tree -r "$review_commit" -- lockstep/engine/src/lockstep lockstep/engine/tests/architecture
git show "$review_commit:lockstep/.superpowers/reviews/2026-08-30-task-12c-architecture-exceptions.md"
```

Populate the closed manifest entry with that ancestor commit, exact
project-relative and `lockstep/` Git-tree paths, artifact blob SHA-256,
reviewed semantic digest, review-evidence digest, metrics, member/source
digests, focused gates, next gate, and all-true expiry fields. Run the
historical recomputation/verifier and repository ratchet. No mixed-
responsibility candidate may enter this cycle.

- [ ] **Step 9: Obtain independent architecture and reliability reviews**

Fix only causal findings, repeat the narrowest RED/GREEN cycle, and require C0/I0/M0 before moving to callers.

- [ ] **Step 10: Repeat Tasks 7.1–7.9 in accepted leaf order**

Finish only when the ratchet reports zero unreviewed candidates and every retained exception is fresh, cohesive, and independently evidenced.

- [ ] **Step 11: Verify the repository ratchet GREEN**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
uv run --no-sync pytest -q
```

Obtain a separate independent GREEN review before starting artifact work.

---

### Task 8: Freeze the Complete Phase 2 Tests-Only RED

**Files:**
- Modify: `engine/tests/workflow/test_schema.py`
- Modify: `engine/tests/workflow/test_semantics.py`
- Modify: `engine/tests/workflow/test_child_lowering.py`
- Modify: `engine/tests/runtime/test_runtime_snapshot_resolver.py`
- Modify: `engine/tests/test_templates.py`
- Modify: `engine/tests/test_template_authoring_write.py`
- Modify: `engine/tests/test_recipe_cli.py`
- Modify: `engine/tests/integration/test_managed_effect.py`
- Modify: `engine/tests/integration/test_native_parallel.py`
- Modify: `engine/tests/runtime/effects/test_acceptance_commitment.py`
- Modify: `engine/tests/runtime/effects/test_parallel_delivery.py`
- Create: `engine/tests/fixtures/controlled_effect_executable.py`

**Interfaces:**
- Freezes the complete artifact grammar, managed projection, exact template
  inventories, production-adapter process lifecycle, recovery, and adversarial
  bearer boundary before any Phase 2 production/resource change.

- [ ] **Step 1: Add artifact grammar and semantic REDs**

Test exact closed shape, safe exact path, write coverage, unique
handle/path/producer, bounded unique headings, call mapping, destination
collision, child export derivation, and preservation of standalone manual
steps.

- [ ] **Step 2: Add managed projection REDs**

Assert exact brief bytes, the SHA-256-derived `managed_brief_` state key,
stable `managed-brief` node, incoming-edge redirection, retry semantics,
descriptor artifacts, result/scope state, exact full capabilities, retained
`artifact_contract`, and current-project snapshot selection.

- [ ] **Step 3: Freeze exact template inventory REDs**

Assert the exact step/call IDs, retry limits, commands, timeouts, writes,
artifact handles/paths/headings, qualified joined handles, destinations, and
runtime requirements from §§5.3–5.4.

- [ ] **Step 4: Add the controlled production-adapter executable**

The test executable reads the real prepared brief/snapshot workspace, emits
bounded fixture results/artifacts, records start/end timestamps, uses nonsecret
binding bytes, and attempts no network access. Tests call public
compile/provision/start and reach `CodexRunnerAdapter.prepare`, durable
commitment, and `ensure_started`.

- [ ] **Step 5: Add reviewed-change lifecycle RED**

Prove one spawn, snapshot provenance, rollover, result, artifact registration,
close/reopen pending acceptance, fresh exact bearer, receipt, publication
bytes, native resume, and terminal completion. Run real local
`pytest -q -p no:cacheprovider`.

- [ ] **Step 6: Add parallel-review lifecycle RED**

Prove two overlapping timestamp intervals, distinct workspaces/artifacts,
partial/batch resume, restart, one join, two separate bearers/receipts, and
terminal completion.

- [ ] **Step 7: Add adversarial acceptance REDs**

Reject PASS text alone, absent/foreign/stale/revoked bearer, wrong
artifact/digest/destination/definition/run/project/coordinate/producer/
descriptor/transformation/audience. Assert no durable fact or destination byte
changes after rejection.

- [ ] **Step 8: Verify the complete correct-reason Phase 2 RED**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/workflow/test_schema.py tests/workflow/test_semantics.py tests/workflow/test_child_lowering.py tests/runtime/test_runtime_snapshot_resolver.py tests/test_templates.py tests/test_template_authoring_write.py tests/test_recipe_cli.py tests/integration/test_managed_effect.py tests/integration/test_native_parallel.py tests/runtime/effects/test_acceptance_commitment.py tests/runtime/effects/test_parallel_delivery.py -q
```

The RED must fail only on missing artifact/managed/template projection, never
on fake adapters, direct provider calls, network, synthetic completion facts,
or unrelated baseline failure.

- [ ] **Step 9: Obtain independent Phase 2 RED reviews**

Require product, reliability, architecture, and threat C0/I0/M0 before any
Phase 2 production or template-resource edit.

---

### Task 9: Implement the Minimal Phase 2 GREEN

**Files:**
- Modify: `engine/src/lockstep/workflow/ir.py`
- Modify: `engine/src/lockstep/workflow/schema.py`
- Modify: `engine/src/lockstep/workflow/semantics.py`
- Modify: `engine/src/lockstep/workflow/lowering.py`
- Modify: seven resources under `engine/src/lockstep/templates/reviewed-change/` and `engine/src/lockstep/templates/parallel-review/`.

**Interfaces:**
- Adds frozen exported-artifact IR with exactly `handle`, `path`, and
  `markdown.sections`; child exports remain separate from parent imports.
- Specialization produces `kind=managed`, selector `codex`, exact sorted
  capabilities, `brief` via `StateSelector`, and
  `snapshot=current_project_snapshot` via `RuntimeInputSelector`.
- `reviewed-change` produces three manual steps, pinned verify, one managed
  review, and one bearer publication; `parallel-review` produces two
  overlapping managed children, native join, and two independent publications.

- [ ] **Step 1: Implement minimal IR/schema/semantic export**

The runtime descriptor remains exactly:

```python
{"name": handle, "source_path": path, "media_type": "text/markdown", "required": True}
```

Remove exact exported paths from `non_artifact_writes`; reject specialization
when any other write remains.

- [ ] **Step 2: Implement minimal managed specialization**

```python
brief = "Task:\n" + task + "\n\nExit criterion:\n" + exit_criterion + "\n"
```

Append only the frozen artifact-path/headings text when exporting. Do not
change runtime snapshot, authority, provider, artifact, consent, publication,
or recovery owners.

- [ ] **Step 3: Implement the seven frozen template resources**

Match the exact reviewed-change and parallel-review inventories frozen by Task
8. Do not change template CLI/MCP grammar, add template paths, or introduce
runtime authority.

- [ ] **Step 4: Run focused, affected, ratchet, and full GREEN**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/workflow/test_schema.py tests/workflow/test_semantics.py tests/workflow/test_child_lowering.py tests/runtime/test_runtime_snapshot_resolver.py tests/test_templates.py tests/test_template_authoring_write.py tests/test_recipe_cli.py tests/integration/test_managed_effect.py tests/integration/test_native_parallel.py tests/runtime/effects/test_acceptance_commitment.py tests/runtime/effects/test_parallel_delivery.py -q
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
uv run --no-sync pytest -q
```

- [ ] **Step 5: Inspect cumulative scope and renames**

```bash
cd lockstep/engine
git diff --name-status -M 4674e43fa1ffef1b9013f29345b2c7934808131e -- src/lockstep tests
```

Inspect every rename and changed path, confirm it belongs to the accepted
Task 12C scope, and report the invariant, ownership, threat-model, and test
evidence for each change. Stop on unexplained scope or semantic variance.

- [ ] **Step 6: Obtain independent Phase 2 GREEN reviews**

Require product, reliability, architecture, and threat C0/I0/M0, including
explicit confirmation that headings/PASS text do not grant authority.

---

### Task 10: Freeze Complete Source/Wheel/Staged-Plugin Gate D RED

**Files:**
- Create: `engine/tests/test_installed_contract.py`
- Modify: `engine/tests/test_plugin_packaging.py`
- Modify: `engine/tests/test_task12_plugin_identity.py`
- Modify: `engine/tests/workflow/test_estimate.py`
- Modify: `engine/tests/test_recipe_cli.py`

**Interfaces:**
- Produces three black-box surfaces: source checkout, clean wheel from a foreign project/venv, and staged plugin containing only tracked delivery paths.
- Each surface exercises both template flows and marker-free manual YAMLGraph through restart/recovery and terminal completion.

- [ ] **Step 1: Add active-byte retirement assertions**

Reject `_subcall`, `lockstep.subcalls`, `_subcall_wrapper.py`,
`Subcalls (v2)`, active fractal/subcall prose, `runners.yaml`,
`LOCKSTEP_RUNNER`, `RunnerSpec`, `load_runners`, legacy runner config,
and `peak_parallel_subcalls`. Exclude only `CHANGELOG.md` and
`docs/superpowers/specs/2026-08-19-codex-claude-parity-design.md`, and assert
that neither excluded file is linked as current guidance.

- [ ] **Step 2: Add clean-wheel isolation**

Build with `uv build`, install the exact wheel into a new temporary environment outside checkout, unset Python path variables, change to a foreign project, and assert every imported module/resource resides in that environment.

- [ ] **Step 3: Add staged-plugin isolation**

Copy only existing packaging-test delivery paths to a temporary root. Run launcher/doctor/serve, CLI/MCP identity, docs/skills checks, both templates, and marker-free manual YAMLGraph from a foreign cwd without checkout import.

- [ ] **Step 4: Require complete public flows on all three surfaces**

Continue beyond start/observe through artifact materialization, pending acceptance, close/reopen recovery, exact bearer receipt, publication bytes, native resume, terminal completion, and parallel overlap.

On every applicable source/wheel/staged surface, assert that each active
example uses the exact current CLI/MCP grammar and compiles. Assert installed
README, `docs/DESIGN.md`, and both skills positively describe the Local
unsandboxed single-user authority model, marker-free manual YAMLGraph path,
`reviewed-change`, and `parallel-review`; configuration and report text
must not be described as authority.

- [ ] **Step 5: Require the renamed estimate contract**

Assert schema v1 exposes only `peak_parallel_child_calls`; no read/write alias exists.

- [ ] **Step 6: Run focused Gate D and confirm tests-only RED**

```bash
cd lockstep/engine
uv build
uv run --no-sync pytest tests/test_plugin_packaging.py tests/test_task12_plugin_identity.py tests/test_installed_contract.py tests/workflow/test_estimate.py tests/test_recipe_cli.py -q
```

Expected: failures only for the frozen legacy production/installed surface. A checkout import, PYTHONPATH leak, adapter bypass, synthetic artifact fact, missing pinned verification, or early-stop smoke is a wrong-reason RED.

- [ ] **Step 7: Obtain independent Gate D review**

Require C0/I0/M0 on source/wheel/plugin isolation, full-flow depth, retirement completeness, and manual YAMLGraph.

---

### Task 11: Implement Minimal Installed-Contract GREEN

**Files:**
- Delete: `engine/src/lockstep/runtime/runners.py`
- Delete: `engine/tests/test_runners.py`
- Modify: exact production/public importers reported by the graph.
- Modify: `engine/src/lockstep/workflow/estimate.py`
- Modify: `.claude-plugin/plugin.json`
- Modify: `.codex-plugin/plugin.json`
- Modify: `.mcp.json`
- Modify: `scripts/lockstep-plugin`
- Modify: active `README.md`, `docs/DESIGN.md`, `skills/lockstep/SKILL.md`, `skills/lockstep-author/SKILL.md`, examples, and packaging assertions.

**Interfaces:**
- Removes `RunnerSpec`, `load_runners`, legacy runner imports/config/env/build argv, and all active subcall terminology.
- Renames only `peak_parallel_subcalls` to `peak_parallel_child_calls` with schema v1 unchanged and no alias.
- Leaves `.mcp.json` with non-authoritative `LOCKSTEP_PLUGIN_HOST=codex`; Claude manifest has no runner environment entry.

- [ ] **Step 1: Query graph impact before deletion/rename**

Use CodeGraph/code-review-graph for importers, callers, tests, affected flows, and blast radius of `runtime/runners.py`, `RunnerSpec`, `load_runners`, and `peak_parallel_subcalls`.

- [ ] **Step 2: Delete legacy runner production and focused test**

Remove only confirmed importers/symbols. Do not add a shim, alias, migration framework, selector, or replacement config.

- [ ] **Step 3: Rename the estimate field**

Change the public key and internal method to `peak_parallel_child_calls`; update active consumers and tests. Retain schema version `1`.

- [ ] **Step 4: Remove host residue**

Remove `LOCKSTEP_RUNNER` and active subcall prose from manifests, launcher,
active docs, skills, examples, and tests. Update every active example to the
exact current CLI/MCP grammar and compile it. Update README,
`docs/DESIGN.md`, and both skills to state the Local unsandboxed single-user
authority model, marker-free manual YAMLGraph path, and real
`reviewed-change`/`parallel-review` flows. Do not edit `CHANGELOG.md` or
`docs/superpowers/specs/2026-08-19-codex-claude-parity-design.md`, and do not
link either as current guidance.

- [ ] **Step 5: Run focused installed GREEN**

```bash
cd lockstep/engine
uv build
uv run --no-sync pytest tests/test_plugin_packaging.py tests/test_task12_plugin_identity.py tests/test_installed_contract.py tests/workflow/test_estimate.py tests/test_recipe_cli.py -q
```

- [ ] **Step 6: Run affected behavior and architecture gates**

```bash
cd lockstep/engine
uv run --no-sync pytest tests/workflow tests/integration tests/runtime/providers tests/runtime/effects -q
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
uv run --no-sync pytest -q
```

- [ ] **Step 7: Revalidate cumulative scope and invariants**

```bash
cd lockstep/engine
git diff --name-status -M 4674e43fa1ffef1b9013f29345b2c7934808131e -- src/lockstep tests
```

Inspect every rename and changed path; report cumulative scope, preserved
invariants, ownership boundaries, threat-model evidence, and test results.
Stop immediately on unexplained variance from the Task 6 semantic path.

- [ ] **Step 8: Obtain independent installed-contract GREEN reviews**

Require product, architecture, reliability, and threat C0/I0/M0.

---

### Task 12: Final Verification, Evidence, and Roadmap Boundary

**Files:**
- Create: `.superpowers/reports/2026-08-30-task-12c-final-evidence.md`
- Modify: `.superpowers/sdd/2026-08-20-lockstep-langgraph-native/progress.md`
- Create: final review artifacts under `.superpowers/reviews/`.

**Interfaces:**
- Produces fresh source, clean-wheel, staged-plugin, architecture, invariant, threat-model, and review evidence.
- Does not authorize publication or Task 13.

- [ ] **Step 1: Run syntax, diff, focused, and full verification**

```bash
cd lockstep/engine
uv run --no-sync python -m compileall -q src tests
uv run --no-sync pytest tests/architecture/test_no_god_methods.py -q
uv build
uv run --no-sync pytest tests/test_plugin_packaging.py tests/test_task12_plugin_identity.py tests/test_installed_contract.py tests/workflow/test_estimate.py tests/test_recipe_cli.py -q
uv run --no-sync pytest -q
cd ../..
git diff --check
```

- [ ] **Step 2: Preserve deterministic scenario evidence**

Record commands, environment isolation, snapshots, production-adapter observations, exact template inventories, overlap timings, artifacts, consent commitments/receipts, publication bytes, recovery events, manual YAMLGraph completion, counts, durations, skips, and digests.

- [ ] **Step 3: Run final independent reviews**

Dispatch product/proportionality, architecture/SRP, behavior/reliability, threat-model, source, wheel, and installed/plugin reviewers. Each artifact reports Critical/Important/Minor and must reach C0/I0/M0.

- [ ] **Step 4: Fix findings through the narrowest causal cycle**

For each finding, return to its owning RED, make the minimal correction, rerun focused plus full verification, revalidate scope/invariants, and repeat that review. Reject scope expansion unrelated to Task 12C's final product contract.

- [ ] **Step 5: Verify completion evidence**

Use `superpowers:verification-before-completion`; confirm fresh outputs rather than historical counts.

- [ ] **Step 6: Stop before external action**

Present exact evidence and remaining risks. Do not publish, push, open an issue/PR, merge, tag, release, bump version, modify marketplace state, run Task 13, or execute post-Task-12 roadmap work without separate approval.

- [ ] **Step 7: Preserve the downstream boundary**

Record that the next action after accepted Task 12C is the mandatory post-Task-12 roadmap reevaluation. Global Task 13 real credentialed Codex acceptance, any newly justified tasks, final `StrEnum` hardening, and the last reusable-analyzer evaluation remain outside this plan.
