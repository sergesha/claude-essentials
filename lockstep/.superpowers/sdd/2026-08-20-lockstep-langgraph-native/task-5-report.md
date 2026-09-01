# Task 5 provider protocols and effect coordinator report

## Scope and threat-model disposition

Task 5 adds the provider-neutral external-runner boundary and crash-safe
coordinator for protected native interrupts. It implements the Local unsandboxed
single-user profile and the SI-12/SI-13/SI-14/SI-23/SI-29 boundaries without
claiming confinement of a runner that already has ambient OS-user authority.
SI-29's ordering is explicit: revocation serialized before the commitment point
blocks start; authorization serialized first wins, and later revocation is
best-effort supervision/reconciliation rather than a claimed rollback.

LangGraph remains the only workflow-state and checkpoint-lineage authority. The
effects ledger contains only external-attempt facts; it adds no work item, route,
join, status, timer, checkpoint copy, grant table, or scope table. All native
access remains behind `GraphRuntime`, and only the yamlgraph adapter imports
yamlgraph/LangGraph or reads their public snapshot/history APIs.

## RED evidence and crash matrix

The initial RED commit `b5e9a19` encoded parked intent, launch adoption and
ambiguity, deadlines, quiescence, sealed delivery, crash recovery, concurrency,
partial/batch resume, wakeups, and scope inheritance. Independent reviews then
added RED cases for:

- provider contact before durable intent or after a lost lease/revision;
- workspace/runner binding drift and request/launch mutation;
- deadline and native-source changes during provider preparation;
- revocation before prepare and immediately before start;
- foreign same-ID direct-subgraph history and changed historical descriptors;
- raw, sibling, foreign, undelivered, or mismatched scope producers;
- aggregate request resource bombs and implicit fake authorization;
- binding rotation after an attempt exists and non-local reconciliation ports;
- unbounded empty history scans and nullable launch commitments; and
- sequential and direct-subgraph checkpoint ancestry through public LangGraph
  parent links.

## Exact request, grant, and provider boundary

`EffectRequest` is closed, detached, aggregate-bounded, and canonically commits
run/project/definition identity, exact native coordinate and descriptor, runner
binding, capabilities, inputs, writes, deadline, and declaration-ordered verified
scope bindings. Each scope binding includes its state key, producer effect and
coordinate, descriptor/scope digest, result digest, and runner binding.

There is no implicit allow authority. `EffectAuthorityGate` returns a canonical
grant binding the actor, project/run/definition/effect/descriptor, trusted runner
and required authorities, workspace, parent generation, policy/config/approval
epochs, generation, and expiry. The durable effect row commits the exact request,
grant, and launch digests. Test fakes deny unknown intents unless an exact grant
was explicitly installed.

The neutral runner port requires idempotent durable preparation/start recovery.
`inspect`, `cancel`, and `quiesce` are restricted to an existing local durable
handle and may not create a new remote/network/credential commitment. Existing
attempts resolve by their durable runner-binding digest, so selector rotation
cannot orphan supervision; new effects still use the current trusted selector.
Provider values remain closed and cannot create graph scope/results, approvals,
grants, deadlines, selectors, or control fields.

## Commitment and crash recovery

Each reconciliation performs one monotonic decision. No-row records persist
intent without provider preparation. Prepared attempts idempotently recover one
durable preparation, then CAS `launching` with its exact launch commitment before
`ensure_started`. Launching recovery adopts the same attempt, reports honest
indeterminacy, or remains authority-blocked after definite absence; it never
starts a replacement.

Immediately before `ensure_started`, `GraphRuntime.commitment_guard` holds the
existing per-thread OS invocation serialization and invoke lease, revalidates the
immutable run binding and exact current interrupt, and exposes the guarded native
snapshot. The coordinator rebuilds the complete request from that snapshot,
rechecks deadline plus the live exact effect lease/revision/phase, and requires
the same canonical request/grant. The authority gate then serializes current
grant/revocation state through the start callback. The two guards cover distinct
authorities and remain nested only across the single commitment point.

After commitment, reconciliation uses only the durable local attempt binding.
Inspection and cleanup never launch. Timeout sealing requires matching
quiescence/result-stability and managed rollover proof; otherwise the attempt
remains running. Expiry before launch seals without provider contact.

## Native lineage and scope provenance

Direct-subgraph delivery no longer relaxes identity to thread plus interrupt ID.
The adapter queries exact namespace-scoped public history, projects historical
interrupts even after their task completes, and accepts only one exact full
coordinate. It bounds both snapshots scanned and occurrences returned. The
coordinator reparses the protected historical descriptor and requires its digest
to match the durable source.

A `ScopeResult` grants nothing merely by appearing in graph state. Its producer
must be the exact delivered scope ledger record in the same bound run/thread,
with matching descriptor, result, state key, and historical protected source.
Additionally, `GraphRuntime` proves the producer checkpoint is an ancestor of
the exact current consumer. The yamlgraph adapter does this using only public
`get_state(exact checkpoint)` and a bounded `parent_config` chain; nested
checkpoint maps anchor parent scopes to direct/nested child namespaces. Sibling
or obsolete scope history therefore fails closed.

## Delivery and wakeups

Only stored sealed results are resumed through `GraphRuntime` against exact
current interrupts, including bounded partial/batch delivery. A row becomes
`delivered` only after the returned native snapshot and exact descriptor-bound
lineage prove commit. Provider output is not graph-visible before resume.

The external deadline reconciler scans at most 128 active rows and retries an
overdue row at a one-second cadence, so sealed/indeterminate or quiescence-pending
work cannot cause a zero-delay busy loop. Clock and wakeup are injected; no
workflow timer state exists.

## Independent review and simplification

Two independent final re-reviews returned **ZERO** findings. They exercised real
LangGraph 1.2.10 sequential, direct-child, and nested-child ancestry, exact
direct-subgraph history, revocation/start ordering, durable binding recovery,
and scope/grant provenance. The final simplification pass removed an unused
context field and redundant grant-derived request fields; it retained only the
separate native serialization and authority-revocation guards required at the
one commitment point.

The final external requirements-conformance review also returned **ZERO** and
confirmed architectural minimality: the changes close the reachable threat-model
deltas without duplicating native workflow state or adding provider-specific
policy to the coordinator.

## Verification

- Focused effects, GraphRuntime, leases, and no-legacy architecture suite:
  `101 passed in 4.91s`.
- Independent focused/native reviews: `102 passed` and `94 passed`, both ZERO.
- Ruff check over every changed Task 5 source/test file: clean.
- `compileall` and `git diff --check`: clean.
- Complete offline repository suite: `572 passed, 3 failed in 89.01s`. The three
  unchanged legacy `junit_gate` tests require a bare `pytest` executable for a
  nested subprocess, while this environment exposes pytest only as
  `.venv/bin/pytest` and `shutil.which("pytest")` returns `None`; all Task 5 and
  dependency-patch tests passed.

No network, push, publication, fork, private saver read, or dependency mutation
was performed.
