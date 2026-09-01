# Task 8 pre-code architecture decisions

Status: **GO**, after the initial independent review returned STOP and a second
independent contract review closed every blocking seam below.

## Frozen boundaries

- Compilation accepts only `ValidatedWorkflow` and is a pure transformation. It
  emits canonical recipe, source-map, and dependency-manifest bytes. Its public
  digest is SHA-256 of the exact recipe bytes.
- Generated metadata is informational. Compiler authority is an in-memory
  `CompilerProvenance` capability, bound to exact recipe bytes and created only
  by immediate compiler output or a byte-for-byte canonical recompile.
- A manual yamlgraph recipe may use any closed protected descriptor without
  compiler provenance. It may not use compiler-only scope/gate/marker surfaces.
- Input selectors are a closed union of `state_key` and the runtime-owned
  `run_start_project_snapshot|current_project_snapshot` keys. Runtime snapshots
  are never fabricated in graph state.
- DSL `verify` remains semantic `kind: verify` while selecting the pinned
  adapter. Its command is `lockstep.pinned-command/v1` with `result_source`, and
  its snapshot input is `runtime_key: current_project_snapshot`.
- `decide` is a distinct, trusted no-spawn descriptor/result contract. It is not
  a runner and does not smuggle its label through an effect-result reference.
- `accept` compiles to a distinct acceptance descriptor but cannot auto-PASS.
  Execution remains fail-closed until the artifact registry and session-bound
  consent API exist in Tasks 10 and 12.
- Generated loop exits always target a deterministic passthrough gate. Only its
  ordinary edge may target a protected interrupt, allowing yamlgraph to rewrite
  the edge through `<interrupt>_prepare`.
- Structural estimates expose every normative count. Controlled time is either a
  sourced bound with formula/assumptions or explicitly unavailable. Human/agent
  end-to-end time is unbounded; token and money estimates remain unavailable
  without complete owner-controlled metadata.

## Explicit exclusions

No runtime scheduler, workflow-state snapshot copy, fake runner, provider-owned
decision label, simulated consent, or effect-kind masquerading is permitted.
Durable runtime snapshot capture/binding, decision execution, and acceptance
execution remain dependencies of their owning later runtime tasks.
