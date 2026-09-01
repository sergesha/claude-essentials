# Task 3 native runtime cutover report

## Scope and threat-model disposition

Task 3 removes the legacy scheduler/state path atomically. yamlgraph remains the
only YAML-to-LangGraph compiler and LangGraph checkpoints remain the only source
of workflow truth. The new runtime layer adds lifecycle, immutable binding,
coordinate validation, and public projection; it does not reproduce yamlgraph
compilation or routing.

The implementation follows the Local unsandboxed single-user threat model. It
closes reachable authority deltas at public ingress, immutable recipe identity,
current native coordinates, invocation serialization, and live session ownership.
It does not add anti-reflection seals to internal frozen Task 2B values: forging
those requires arbitrary Python inside the orchestration TCB and is outside the
stated boundary.

## RED and hostile witness

The required RED suite was captured before implementation. Runtime/status/hook
projection modules were absent, while the legacy public path was still live.
The hostile `scenario_start` witness used a recipe-selected Python module whose
import wrote a sentinel. The old public call compiled and executed the Python
node before refusing the transition, proving the Task 2B authority seam had not
yet replaced production execution.

Additional focused RED evidence was captured for:

- stream iteration without an invocation lease (a competing owner acquired the
  same thread lease while the stream was live);
- policy admission after a transitive child definition changed;
- the MCP-edge/session-check race, with ownership changed between the edge check
  and service resume;
- `scenario_dryrun` reading a live executable recipe without immutable authority
  admission; and
- the initially missing GraphRuntime batch-resume coverage for parallel native
  interrupts.
- a stale session owner racing a concurrent rebind at the native-resume cut point;
- TTL-only invocation ownership, nested parent/child policy ambiguity, and
  forged engine-owned start/outcome fields;
- rejected first-call start/dry-run creating persistent owner state; and
- insecure hook SQLite/materialization reads, session-ID disclosure, and stale
  bindings reported green;
- cross-project status/resume authority, mutable read-only recipe tools, and a
  duplicated expiring session-lock implementation; and
- unbounded JSON ingress, checkpoint-history projection, and lineage scans.

The final hostile start regression proves rejection before the import sentinel,
catalog row, or checkpoint database exists. Validation and dry-run regressions
apply the same default-deny executable authority before yamlgraph compilation.

## Native runtime and public projection

`GraphRuntime` binds each public run to its immutable Task 2B bundle and native
thread. It exposes only `bind`, `start`, `snapshot`, `history`, `resume`, and
`stream`, validates exact current checkpoint namespace/checkpoint/task/interrupt
coordinates plus native lineage and accepts native interrupt-ID batches. A
kernel-managed POSIX advisory lock spans invoke, resume, or the full lifetime of
stream iteration; the database lease is acquired only inside that crash-released
cross-process fence, so TTL expiry cannot admit a concurrent native commit. Apps
and SQLite connections are closed on normal teardown, failed
service starts, explicit unbind, restart, and multi-app close paths.

`project_status` derives the six public values (`starting`, `awaiting`,
`running`, `completed`, `escalated`, `aborted`) from native snapshot facts.
RunCatalog stores immutable discovery bindings only and has no status column.
The state-free compatibility-named `Engine` delegates to `LockstepService` and
owns no workflow transition or persistence logic.

`LockstepService` owns strict start admission, immutable materialization,
catalog binding, native status/history, and the generic worker result controls.
Each mutation selects the exact current worker-owned native interrupt and holds
the session binding's mutation lock from verified ownership through the native
resume commit. It supplies a closed worker result shape and never copies a public
status into another store. Start rejects engine-owned inputs before mutation;
status accepts terminal outcomes only when native pending/next coordinates are
empty and fails closed on unknown terminal outcomes. Public status/history and
controls require the exact caller project; foreign and unknown run IDs have the
same response and rejected calls do not mutate checkpoints.

Public start/result/reason values cross one generic bounded JSON boundary before
state lookup or initialization: depth, node count, scalar/canonical UTF-8 bytes,
integer range, finite-number, Unicode, and JSON-domain type ceilings are all
finite. Public native history and resume lineage consumption are independently
capped at 1,024 snapshots with stable fail-closed errors.

## Hook and server migration

MCP start and dry-run execute pure strict-ingress/profile preflight before the
lazy service can initialize owner state or SQLite. Validation and rendering also
use strict ingress and authorized immutable materialization before any yamlgraph
operation. Recipe listing and rendering resolve configuration without constructing
the persistent service. The server uses one authenticated session identifier
through its edge check and service resume; the service binding lock closes the
check/use race. History and listings are native/catalog projections rather than
fabricated route logs or RunIndex rows.

Hooks and doctor are verified read-only projections over RunCatalog and native
checkpoints. They verify owner-only state, SQLite main/journal/WAL/SHM files,
bundle manifests, and complete immutable materializations without creating
storage. Failures are redacted; Stop/SessionStart remain fail-open and PreTool
remains fail-closed. Checkpoint connections use SQLite read-only immutable mode.
PostTool binds only a newly started, real current worker-awaiting public run;
status never refreshes or adopts ownership. PreTool requires
an exact complete recipe-definition digest, including transitive sources, plus
the exact live session owner. Nested policies choose the longest matching
project, and a run must belong to that exact policy project while cwd may be a
descendant. Doctor never prints a complete session ID or reports stale ownership
green. The MCP edge and the final resume commitment both require an exact,
non-stale binding; public awaiting status exposes only the redacted
`binding_integrity=missing_or_stale` failure. Native child graphs have no child public run,
nonce, ancestry, credential, or environment identity.

## Legacy disposition

The legacy `runs.py`, `subcalls.py`, `_subcall_wrapper.py`, runs.json config,
Engine scheduler/transitions, raw adapter compiler/validator methods, child
credentials, subprocess-child budgets, and legacy subcall profile exceptions
were removed. Obsolete subprocess lifecycle, RunIndex, daily-change, and subcall
fixtures/tests were deleted rather than hidden behind compatibility flags.

The architecture tests parse every production module and reject RunIndex,
ACTIVE_STATUS, `runs.json`, raw legacy compile/validate calls, or yamlgraph /
LangGraph imports outside `yamlgraph_adapter.py`. The legacy-test disposition
test also rejects reintroduction of the removed lifecycle suites.

## Verification

All commands were run from `engine/` unless noted. Final counts and the
independent-review verdict are recorded after the final review/fix pass.

- Expanded focused Task 3 command: `134 passed in 12.26s`.
- Complete offline code and packaging suite: `489 passed in 88.73s`.
- Ruff over every new or substantially rewritten Task 3 production/test file:
  `All checks passed!`.
- Architecture AST/rg guards: `3 passed in 0.29s`; `compileall` and
  `git diff --check` completed cleanly.

Repository-wide Ruff also reports pre-existing debt in unrelated Task 2/8-era
files. The Task 3 surface is clean; this cutover deliberately avoids mechanical
rewrites outside its architecture boundary.

No network access, publication, push, or upstream mutation was performed.

## Independent review

The first independent review returned seven findings: session TOCTOU, TTL-only
invocation fencing, nested-policy ambiguity, input/status spoofing, persistent
initialization before admission, unverified hook projection/doctor disclosure,
and singleton connection leakage. The fresh re-review then found the expiring
session lock, missing project scope, duplicated lock TCB, mutating read-only
recipe tools, and unbounded ingress/history. Each was reproduced with a focused
RED test and fixed at its earliest authority boundary. Final fresh-context
verdict: **ZERO** (no P0, P1, or P2 findings).

An external final review then identified two blockers: expired bindings were
identity-checked without a liveness check at commitment, and dry-run evidence
was bounded only after recipe preflight. Both now have deterministic negative
oracles: stale status is redacted, stale done/escalate/abort leave checkpoints
byte-identical, status cannot adopt, and dry-run rejects oversized/deep/non-object
evidence before recipe lookup or persistent state. External re-review is pending
on the follow-up fix commit.

The next external pass found one final expiry edge: PreTool refresh accepted an
exact but already-stale owner and rewrote `last_seen` before the service gate.
Refresh now receives the configured stale window and, under the shared advisory
lock, requires both exact identity and a live binding before any write. The
deterministic regression proves denial, byte-identical stale binding, and a later
resume rejection. External re-review of `7ae7812` returned **ZERO**: no remaining
P0, P1, or P2 findings.
