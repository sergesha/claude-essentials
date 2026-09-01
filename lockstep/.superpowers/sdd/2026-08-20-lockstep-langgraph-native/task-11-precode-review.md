# Task 11 pre-code design and conformance review

Date: 2026-08-21

Decision: **GO**, subject to the frozen seam below.

The review was performed independently before production edits, against the
threat model, native design, Task 10 report, and Task 11 plan.

## Frozen authority seam

- yamlgraph/LangGraph owns the static branch topology, native pending tasks,
  reconvergence, and join. Lockstep adds no branch scheduler, progress table,
  join row, timer row, or branch status machine.
- A parallel lowers to one declaration-ordered list fan-out, distinct
  branch-local outcome channels and completion gates, and one native
  multi-source reconvergence. The closed join precedence is `ABORTED > ERROR >
  FAIL > PASS`; no branch failure may cancel a sibling early.
- Static fan-out remains bounded by the existing DSL limits (2–8 branches, no
  nested parallel). Cross-branch artifact destinations must be portable and
  pairwise non-overlapping, including ancestor and case/normalization aliases.
- A bounded parallel first parks at one no-spawn `parallel` ScopeDescriptor.
  Branch effects inherit that result plus lexical ancestor scopes. Effective
  deadlines remain the minimum of own and ancestor bounds. Unbounded parallel
  emits no timer or synthetic wakeup.
- Manual work hidden behind graph/include is invalid in a bounded parallel.
  A timeout fact may seal only under the existing Task 9/10 terminal-safety and
  quiescence rules; scope ERROR bypasses fan-out.
- Concurrent delivery is a bounded sweep of the current protected native
  coordinates. Each coordinate receives at most one monotonic reconciliation
  decision per sweep. Exact leases and commitment guards remain per effect;
  delivery may batch exact sealed interrupt facts, but only LangGraph closes the
  join.
- Restart/due recovery is coordinate-based, not deduplicated by public run.
  Flat completed-task metadata must never resurrect a consumed branch.
- Public status is a bounded, redacted aggregation of all current pending tasks
  and durable effect facts. It is observational and introduces no authority.
- Task 10 ArtifactRefs and registry provenance pass through branch results
  unchanged.

## Pre-code blockers closed by the design

1. ParallelIR had no native lowering.
2. Existing failure routes bypassed any future join.
3. Lowering lacked lexical scope and branch outcome contexts.
4. Cross-branch artifact overlap and hidden bounded manual effects were not
   closed across every accepted boundary.
5. Coordinator selection of the first active record could starve sibling native
   tasks; due recovery also deduplicated by run.
6. Status projected only the first pending interrupt.
7. DecisionDescriptor execution still lacks the dedicated durable runtime
   snapshot resolver. Parallel semantics therefore rejects DecideIR rather than
   pretending it can execute. VerifyIR preserves its runtime selector and still
   fails closed at that explicit Task 8 boundary.

Production code was authorized only after RED tests captured this seam.
