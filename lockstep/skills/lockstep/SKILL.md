---
name: lockstep
description: Use when a lockstep run is active or the user asks to run a scenario
---

# lockstep

You are working a **lockstep run**: a step-by-step recipe whose engine validates every
report deterministically — commands, file hashes, junit output — never your say-so. Reporting
a step does not mean the step is accepted; it means the engine re-checks it. Work the loop
below exactly; do not freelance around it.

## The loop

1. **`scenario_status(run_id)`** — read the current step's `task` and `exit_criterion`. This is
   the source of truth for what to do next, every time, even right after `scenario_start`.
2. **Do ONLY the current step.** Not the next one, not a shortcut across steps. The recipe's
   step order is the point of lockstep — working ahead produces evidence the current step's
   checks were never designed to accept, and the engine has no path to credit it.
3. **Collect evidence as artifact pointers**, not prose. A step's `evidence_schema` tells you
   exactly which keys it wants and their shape (commonly a `project-path` string — a path
   relative to the project root, e.g. `"docs/plan.md"`, never absolute and never `../`-escaping
   the project). Produce the real file/artifact first, then report its path. Evidence is a
   **pointer** the server goes and inspects/re-runs against — never a claim you assert in text.
4. **`scenario_done(run_id, step, evidence)`** — submit. The response tells you what happened;
   read it before doing anything else:
   - `passed: true` → advance; the response carries the next step's `task`/`exit_criterion`
     directly (or `done: true` if the recipe is complete). Loop back to step 1's mindset — trust
     the response, but a fresh `scenario_status` is always safe if anything is unclear.
   - `passed: false` (no `error`) → read `reasons` — they name exactly what the checks found
     wrong. Fix the real thing the reason describes, then resubmit `scenario_done` with new
     evidence. Do not resubmit the same evidence unchanged. Each recipe step has a limited
     number of allowed attempts before the run auto-escalates — do not burn attempts guessing;
     read the reason, fix that specific thing.
   - `error: true` → this is NOT a failed check — it's the check machinery itself breaking
     (a crashed command, a missing baseline, a schema it couldn't evaluate). It does **not**
     consume a retry attempt. Report the infra problem in your own words to the user/operator;
     do not just resubmit and hope, and do not treat it as "one more failure" against your
     attempt budget.
5. **`scenario_escalate(run_id, reason)`** — call this yourself when you are genuinely blocked
   (missing credentials, an ambiguous requirement only a human can resolve, a recipe step that
   cannot be satisfied as written) rather than burning retries or inventing evidence. Escalating
   honestly is always the right move over guessing.

## Evidence rules

- **Pointers, not claims.** Never report "tests pass" as prose — report the path/command the
  recipe's schema asks for and let the server verify it. The server re-runs the pinned
  commands and re-hashes the files itself; nothing you say substitutes for that.
- **`_`-prefixed evidence keys are reserved and rejected outright**, before any other
  validation runs. Never invent a key starting with `_` (e.g. `_verdict_status`) — that
  namespace is engine-internal, and submitting one gets your whole `scenario_done` call
  refused with an error, not silently ignored.
- Evidence paths are **project-relative**. The server resolves them against the run's project
  root and rejects anything that would escape it (`../../etc/passwd`-style paths, absolute
  paths where a `project-path` is expected).
- Commands a step runs are **pinned in the recipe** — you cannot supply or influence which
  command runs via evidence. If a check looks wrong for the situation, that's a reason to
  escalate, not to work around it.

## Subagent pattern

For steps whose evidence is a review/analysis artifact (e.g. a code-review markdown file),
dispatch the work via your own `Agent` tool rather than writing it inline yourself — a second
independent pass is worth more than a self-review. The dispatched agent's output file, saved to
a project-relative path, IS the evidence: point `scenario_done` at that file. The lockstep
server does not know or care that a subagent produced it — it checks the artifact exactly like
any other.

## Subcalls (v2) — when a step parks on an independent session

Some steps park on a **subcall marker** (`step: _subcall` in `scenario_status`):
the engine has spawned a separate CLI agent session — possibly running its
own lockstep child run — in its own process, outside this conversation.
While a subcall is in flight:

- **`scenario_status`/`scenario_done` auto-poll it for you** — you never
  drive the poll loop yourself. Call `scenario_status` to see whether it is
  still `running` or has reached `done`/`error`; do not fabricate progress
  or guess at its outcome. Liveness only advances on a tool call —
  `scenario_status`/`scenario_done` — nothing polls in the background, so a
  subcall makes no progress until you (or the parent's own retry) call one
  of them. One exception you get for free: a fractal child's own terminal
  report nudges its parent, so a parent parked on a finished child advances
  without you.
- **`scenario_done` is refused while a subcall is running** — read the
  refusal message; it names the subcall's node and how long it's been
  running. There is no step of your own to report evidence for yet — poll
  again instead.
- **`scenario_abort`/`scenario_escalate` are ALSO refused while the subcall
  runs.** This is deliberate, documented behavior, not a bug: a poisoned
  child session must not be able to talk you into killing its own parent
  run. The refusal names the escape hatch — the subcall's runner timeout is
  finite; once it fires the subcall resolves to an error envelope and
  abort/escalate become available again.
- You cannot read the spawned session's transcript or influence what it is
  told from here — that independence is the entire point of a subcall. See
  README.md "Subcalls (v2)" for the precise guarantee this gives you and,
  just as importantly, what it does NOT give you.

## Terminal statuses

`escalated` and `aborted` are **terminal** — there is no resume path in v1. Once a run lands in
either status, `scenario_done` on it will simply fail with "terminal". A genuinely blocked run
stays blocked until a human looks at it and starts a **new** run (`scenario_start`) — do not
try to route around a terminal run by inventing workarounds.

An `awaiting` run is also **session-bound**: in a policy-gated project the write gate opens
only for the session driving the run — yours automatically, from the moment your session
called `scenario_start`. Your ordinary work keeps that claim alive; nobody can take a run
from a session that is still working. If the write gate denies you because a run belongs to a
session that died (a crash, a closed window), the run falls silent, and after
`LOCKSTEP_SESSION_STALE_MINUTES` (default 30m) of silence a `scenario_status` call on it from
your session **adopts** it — then continue it as your own. If you cannot wait out the window,
`scenario_abort` the run and `scenario_start` a fresh one, which is bound to you at birth. A
run whose driver is live can never be adopted — the deny message means that session's work is
in flight, not that the gate is broken.

## Never end a turn with an active run unreported

If `scenario_status` (or your own memory of the conversation) shows an `awaiting` run whose
current step you have already worked, report it via `scenario_done` before ending your turn.
An unreported active run is exactly the situation the Stop hook exists to catch — don't rely on
it; report proactively. If you are not yet done with the step's actual work, that's fine —
just don't sit on finished work unreported.

## What NOT to do

- Do not edit the lockstep state dir or recipe files while a run is active — `scenario_start`
  snapshots the recipe at run start; live edits to the source recipe are inert for that run
  anyway, and touching the state dir is outside your job regardless of whether a permission
  system is blocking it.
- Do not invent step names. Call `scenario_status` and use exactly the `step` value it returns
  when calling `scenario_done` — a mismatched step name is rejected.
- Do not argue with the validator. A `fail` verdict's `reasons` are the ground truth about what
  the checks found; you cannot appeal the checks, only fix what they describe. If a check is
  genuinely wrong for the task, that is a reason to escalate to a human, not to keep resubmitting
  or to fabricate evidence that happens to satisfy the regex/hash/exit-code.
