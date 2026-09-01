# Task 8 deterministic DSL lowering report

## Progress ledger

1. **Pre-code architecture:** initial independent review returned STOP. Exact
   runtime-selector, decide, accept, provenance, compilation-artifact, loop-exit,
   and estimate contracts were frozen by a second independent review. Expanded
   RED tests then received final pre-code **GO**.
2. **RED:** parser legacy-surface rejection; exact descriptor/result variants;
   canonical recipe/source-map/manifest bytes; freshness/provenance binding;
   sequence, choose, retry, repeat, terminals, real yamlgraph exit-gate prepare;
   manual/native profile split; structural estimates and honest unavailable
   semantics.
3. **GREEN:** pure canonical/compiler/lowering/freshness/estimate modules;
   closed runtime selector and decide/accept models; provenance-aware profile;
   native control-flow lowering; admitted-binding lineage correction.
4. **Verification:** focused workflow/profile/runtime gates, complete offline suite,
   compileall, diff check, and independent final review.

## Architecture delivered

Compilation accepts only semantic `ValidatedWorkflow` input and returns immutable
canonical recipe, source-map, and dependency-manifest artifacts. Stable node IDs
derive from source pointer/kind/role, while freshness separately binds the exact
raw workflow source digest. Compiler modules do not import yamlgraph, LangGraph,
Engine, service, storage, ledger, or scheduler types.
Checked-in representative control-flow goldens compare all three compiler
artifacts byte-for-byte, so a deterministic semantic drift cannot pass merely by
repeating the same changed compilation twice.

Sequence, manual steps, pinned-adapter verify, trusted decide, accept contracts,
choose, retry, bounded repeat, and graph-owned terminal outcomes lower to native
yamlgraph nodes and edges. Attempt gates precede effects. Generated `loop_exits`
target passthrough gates; a real yamlgraph regression proves the ordinary edge to
a protected interrupt executes its generated prepare node with the complete fresh
descriptor. No workflow scheduler, duplicate status, fake runner, snapshot copy,
or simulated consent was introduced.

Profile admission now has two explicit paths. Complete manual yamlgraph may use
ordinary closed protected descriptors without generated authority. Compiler-only
markers and scope require an in-memory exact-byte `CompilerProvenance`, issued
only for immediate compiler output or a verified byte-for-byte canonical match.
Edited/foreign bytes and direct loop exits to interrupts fail closed.

DSL `verify.writes`, `verify.junit`, and legacy accept `artifact + hash_from` were
removed from parser/IR/semantics. `verify` remains semantic `kind: verify`, uses
the pinned adapter and exact `lockstep.pinned-command/v1`, and selects the
runtime-owned current snapshot without copying it into workflow state. Decide and
accept have distinct immutable descriptors/results. Their later runtime snapshot,
artifact-registry, and session-consent execution boundaries remain fail closed
and are owned by the later tasks identified in the native plan.

Structural estimates expose every normative count. Controlled time is a sourced
bound with formula/assumptions only when every required timeout is known;
otherwise it lists exact unavailable reasons. Human/external-agent end-to-end
time is unbounded. Token and money estimates remain unavailable without complete
owner-controlled metadata.

## Explicit later-task release gate

Task 8 intentionally does not grant generated recipes a provenance-free public
`Engine.start` path. Task 12 owns the source-selection and freshness boundary.
Before generated workflow start is releasable, `tests/test_recipe_cli.py` must
prove that checked-in workflow source is recompiled, exact recipe bytes match,
strict-ingress canonical materialization is rebound to
`CompilerProvenance(context="canonical-match")`, and changed/foreign bytes are
rejected. Until that test and wiring exist, a generated marker presented through
the public manual start path remains rejected. Immediate compiler output and
exact canonical-match profile admission are already covered in Task 8.

## Verification evidence

- Expanded Task 8 workflow/descriptor/profile/runtime gate: `178 passed`.
- Complete offline suite: `718 passed, 1 skipped` in `111.25s`.
- `python -m compileall -q src tests`: clean.
- `git diff --check`: clean.
- Ruff could not be executed because no Ruff binary or project dependency exists
  in the offline environment; this is reported rather than silently claimed.

No network access, dependency mutation, push, publication, fork, PR, or GitHub
operation was performed.
