# lockstep v2 — sub-invocations (subcalls) design spec

Date: 2026-08-07, rev 4, corrected to AS-BUILT after implementation.
Status: describes the shipped v2 code; where the original design and the
code diverged, this file states what the code does (the "As built" list
below names every divergence).
Base: lockstep v1 (branch `feat/lockstep`, tag `lockstep-v1-checkpoint`,
119 tests, review CLEAN). Work continues on the same branch; v1 tag is the
squash boundary. Final home: `claude-essentials/lockstep/docs/` beside
DESIGN.md.

## As built — divergences from the pre-implementation design

- **`copilot` stays forbidden.** The conditional shim-runner allowance
  ("copilot leaves the forbidden list when the shim runner is enabled")
  was NOT built: `copilot` remains in `FORBIDDEN_NODE_TYPES`
  unconditionally, and no PATH-shim mechanism exists. Deferred (README
  "Explicit deferrals").
- **Runner sessions are never resumed.** The envelope's `session_id` is
  informational; `safe_argv`'s resume gate exists only for a future
  continuation path.
- **Model selection**: the engine always launches with `models[0]` from
  the runner spec; nothing in the recipe dialect selects a model. Later
  entries are reserved.
- **Process model**: there is no pidfile-based liveness or kill. A small
  supervisor (`_subcall_wrapper.py`) owns the child handle and records
  the terminal verdict in `<workdir>/exit.json` (first writer wins);
  termination is requested by touching `<workdir>/cancel`, never by pid.
  A probe past the deadline with no verdict claims `timed_out` itself.
- **Start-time policy**: runner resolution (allowlist, absolute
  executable path, non-empty models) IS checked at `scenario_start` for
  every subcall marker in the snapshot — refusing the run before any
  work. Budgets and fractal depth are enforced at `done()` time,
  engine-side before any resume (they depend on runtime state).
- **Child recipe pinning** (beyond the original design): every fractal
  child recipe a marker names via `scenario:` is snapshotted at the
  PARENT's start (`runs/<parent>.child.<scenario>.yaml`), profiled from
  that copy, and the child run launches only from it — a live edit to a
  child recipe after the parent started is inert.
- **Gate linkage, as implemented**: `hook_pretool` keeps the v1 predicate
  for sessions without `LOCKSTEP_CHILD_RUN` in their environment (an
  awaiting policy-recipe run unlocks the project). A spawned child
  session carries `LOCKSTEP_CHILD_RUN`; for it the hook walks that run's
  `parent_run` chain and unlocks ONLY while every run on the chain is
  awaiting and the root is an awaiting run of the policy recipe in the
  project. Both predicates additionally enforce run EXPIRY (`updated`
  within `LOCKSTEP_STALE_HOURS`, default 24h): an expired awaiting run
  is dead and no longer satisfies the gate — the deny names the only
  working exit, `scenario_abort` then a fresh `scenario_start`;
  `scenario_status` never refreshes `updated`, so it cannot reopen the
  gate (README, policy gate). Honest caveat: a finished child's
  `_nudge_ancestors` poll advances a parent parked on that subcall and
  stamps its `updated` fresh — restarting the expiry clock in exactly
  the dead-worker case expiry targets; the real fix is session binding,
  a separate piece of work.
- **Child identity, as implemented**: `LOCKSTEP_CHILD_RUN`/
  `LOCKSTEP_CHILD_NONCE` env + an ENGINE-generated preamble prepended to
  the marker prompt naming the child run id and the report path
  (scenario_status/scenario_done on that run); the SessionStart hook
  additionally marks the run matching the session's `LOCKSTEP_CHILD_RUN`
  as "THIS SESSION'S OWN child run".

## Goal

Two capabilities through ONE mechanism:

- **(B) Independence**: a recipe node whose work is done by an
  ENGINE-spawned CLI agent (`claude -p`) in a separate session — a channel
  the main (worker) agent cannot read or influence. First customer: a
  genuinely independent review gate (v1's reviewer-nonce was rejected as
  theater precisely because the worker spawned its own reviewer).
- **(A-via-B) Fractal composition**: the spawned session can itself run
  under a lockstep scenario — a CHILD RUN with its own checkpoint, snapshot,
  baseline and gates. The parent's evidence is the child run's terminal
  status: machine-checked by construction, not the child's word.

Explicitly NOT the model: global inversion. The main agent stays a live
session owning open-ended work; subcalls take CLOSED sub-tasks (known
route, checkable output).

## Verified foundations (installed yamlgraph 0.5.18 + langgraph 1.2.10;
explored, then adversarially verified for false positives — 2026-08-07)

1. The `python → interrupt → python` loop pattern (interactive_tool's
   expansion) has REAL, restart-durable top-level interrupt/resume —
   proven across three separate processes on SqliteSaver. This is the
   substrate.
2. `subgraph` is broken as shipped for interrupt-bearing children (both
   modes). Root cause is small: `_maybe_wrap_otel` wraps nodes as
   `(state)`-only, stripping the `config` param (child runs
   checkpointer-less → interrupt returned in-band → dropped by the
   reserved channel), and blindly calls `CompiledStateGraph` in `direct`
   mode. With the wrapper neutralized, invoke-mode fractal subgraphs work
   end-to-end (proven by monkeypatch probe). → upstream issue, not our
   code.
3. `copilot` node's argv is hardcoded (`["copilot", "--silent", ...]`) —
   but a PATH-shim executable named `copilot` translating to
   `claude -p --output-format json` was proven end-to-end through the
   stock node (incl. `--model` passthrough and session-id scrape via the
   shim-written `--share` file; shim must emit stdout — empty stdout +
   "error" in stderr triggers the node's deliberate RuntimeError).
4. yamlgraph has NO process management (no detached spawn, no pid
   registry, no cancel; node timeout detaches without killing). Process
   lifecycle is unavoidably ours — and only that.

## Mechanism: the subcall triple (maximum reuse, minimum invention)

A subcall is a recipe-level triple using ONLY existing node types:

```yaml
  review_spawn:                # python — our hook, same channel as run_checks
    type: python
    tool: subcall_spawn        # lockstep_mcp.subcalls:spawn
  review_wait:
    type: interrupt            # NATIVE park; message carries the subcall MARKER
    message: {step: _subcall, node: review, runner: claude}   # marker brief
    state_key: brief
    resume_key: evidence
    idempotent: false
  review_poll:
    type: python
    tool: subcall_poll         # lockstep_mcp.subcalls:poll
```

with a native `loop_until`-style conditional edge poll→wait while running,
and poll→next when the subcall completed (result envelope in state).

Division of labor:

| Piece | Owner |
|---|---|
| Park, checkpoint, resume, poll loop, loop exit | yamlgraph/langgraph native (proven) |
| Triple structure | recipe convention (profile-checked, like step triples) |
| `subcall_spawn` / `subcall_poll` hooks | plugin module (`lockstep_mcp.subcalls`): spawn the child under a supervisor (`_subcall_wrapper.py`) via stdlib `subprocess` (portable options only — no `setsid`/`creationflags` platform branches); `proc.json` + captured output + first-writer-wins `exit.json` under `runs/<id>.subcalls/`; termination by touching the `cancel` file (the supervisor owns the handle — no pid-based liveness or kill); reattach after server restart is files-only |
| ONE new engine rule | a run parked on a `{step: _subcall}` marker is AUTO-POLLED on `scenario_status`/`scenario_done` entry (engine resumes with an internal `{_subcall_poll: true}` tick — `_`-prefixed = engine-internal channel, consistent with the verdict convention) instead of being served to the agent as a brief; a worker's `scenario_done` while a subcall is in progress is REFUSED with "subcall in progress: <node>, <runner>, <minutes>m"; status reports `{subcall: {node, runner, running_minutes}}` |

Liveness model (honest, review M2): polling happens ONLY on
`scenario_status` / `scenario_done` entry — hooks are read-only on
runs.json (v1 contract, unchanged) and `list_runs` reads the index raw;
neither polls. A sleeping run makes no progress until someone calls
status — with one engine-internal exception: a child run's terminal
transition, the last instant its session is provably alive, best-effort
polls each ancestor once (`Engine._nudge_ancestors`), so a parent parked
on that subcall advances even when its own worker session is dead. The
Stop hook gets subcall-aware text: on a run parked in a
subcall it says "subcall in progress — check scenario_status", never
"report via scenario_done" (which is refused during a subcall).

Result envelope (target shape = CopilotResult precedent), state keys
NAMED and `_`-prefixed (review M4 — inherits both v1 anti-forgery
rejects for free: `_`-vars refused at start, `_`-evidence refused at
done; yamlgraph's condition grammar accepts leading underscores —
verified): `_subcall_status` (running|done|error), `_subcall_envelope`
`{output, exit_code, session_id, child_run?, child_status?,
artifact_hashes}`. Both keys MUST be declared in the recipe `state:`
block — LangGraph drops undeclared channels; profile requires it.

**Artifact-hash provenance — hashes come from the DENIED side, never
from collect-time project bytes** (review F1, the round-1 M1 fix
guarded the wrong side): the worker owns the project dir and the parent
run is `awaiting` throughout (gate unlocked), so any hash taken from
project files at collect is a hash of possibly-forged bytes.

- **Fractal child**: `artifact_hashes` are COPIED from the child run's
  final validated baseline snapshot (`runs/<child>.baseline.<n>.json`,
  written by the CHILD's engine at its last PASS, inside the
  agent-denied state dir). The parent trusts the child's own gates, not
  the project tree. Precisely: the digest pins the artifact bytes AS OF
  the child's last validated PASS — the worker can author them up to
  that instant, so the pin is provenance, and the child's checks plus
  the parent's own content checks are what vouch for content. **Required rule** (review round-3 major-1): every
  artifact a parent names in `hash_from: _subcall_envelope.
  artifact_hashes.<name>` MUST be covered by the CHILD recipe's
  `baseline_globs` — else there is no hash to copy and the check errors
  (loud/fail-closed, but inert in exactly its target scenario).
  Enforced at start-time: when a recipe declares a fractal subcall, the
  engine checks the child recipe's globs cover the parent's referenced
  artifacts (both snapshots are available — parent compiled, child
  recipe resolvable).
- **Non-fractal one-shot runner**: the validated artifact is the
  runner's CAPTURED OUTPUT file under `runs/<id>.subcalls/` (state
  dir), not a project path — the reviewer's verdict is read from where
  the worker cannot write.

`from_state:` check args — **pulled into v2 scope** (review M3/F2b; v1
backlog item 1d) WITH the delivery path named: at `scenario_done`, the
engine reads graph state via `yamlgraph_api.peek(app, run_id).state`
(returns checkpoint values — verified) and resolves `hash_from:`/
`from_state:` paths from it ENGINE-SIDE before running checks (the
envelope lives in graph state, invisible to the check dict otherwise —
this sentence is the fix, without it an implementer wires it to
nothing). A check e.g.
`{type: file_matches_hash, path_from: <state-dir output>, hash_from:
_subcall_envelope.artifact_hashes.<name>}`; exact syntax fixed at plan
time. Everything else about evidence rules is unchanged.

## Runner model

- **Registry in the engine**: contract = spawn / poll / collect →
  envelope. v2 registers ONE implementation: `claude`
  (`claude -p --output-format json`, model param, optional
  `--resume <session_id>` continuation).
- **Absolute paths only** (review C2): `runners.yaml` pins the
  executable's ABSOLUTE path per runner; `subcall_spawn` execs that path
  and NEVER PATH-resolves — on no-sudo deployments `~/.local/bin` is
  agent-writable and on PATH, so PATH resolution inside the engine lets
  the worker plant a fake `claude` that forges the "independent" session.
  For the stock-copilot-node path (argv hardcoded upstream): when the
  shim runner is enabled, the engine sanitizes the node's subprocess PATH
  to a single owner-controlled shim directory; the shim is installed by
  the OWNER into an agent-unwritable directory (README consumer
  guidance names it beside the state-dir deny).
- **Resolution**: node's optional `runner: <name>` → default from the
  ADAPTER (`LOCKSTEP_RUNNER` env in plugin.json — "same as the main
  harness" by construction, no detection heuristics; codex adapter would
  set `codex`) → **owner allowlist** `$LOCKSTEP_STATE_DIR/runners.yaml`
  (policy.d trust pattern: agent-unwritable).
- **Cross-runner future** (codex from claude and vice versa): recipes
  already may say `runner: codex`; enabling it later = new registry impl
  (plugin release) + one owner line in runners.yaml. Recipes unchanged.
- **No silent substitution**: unavailable runner → loud start-time
  refusal (cross-vendor review is intentional; silent swap breaks intent).
- **Profile rules**: `runner:` must match `^[a-z][a-z0-9-]*$` (name only,
  never a command); recipes never define runner commands.
- `runners.yaml` also carries budgets: `max_subcalls_per_run`,
  `max_fractal_depth` (default 2), `timeout_minutes` per call (finite,
  profile-required), `models` allowlist per runner. Engine defaults
  exist; owner may tighten. Enforcement is ENGINE-SIDE before resume
  (review M7). Runner availability/config is checked at START-TIME
  against the snapshot (review m3): `validate_recipe` results are
  host-dependent by design (runners.yaml is owner policy, same trust
  pattern as policy.d) — stated, not accidental; mid-run runners.yaml
  edits do not affect a compiled run except that the ABSOLUTE exec path
  is re-read per spawn (owner tightening takes effect immediately).
- The `copilot`-node PATH-shim alternative for short one-shot prompts was
  NOT built (see "As built" above): `copilot` stays in
  `FORBIDDEN_NODE_TYPES` unconditionally.

## Fractal child runs

**Where fractal config lives (corrected 2026-08-07 by probe):** yamlgraph
0.5.18 REJECTS unknown keys in a node's config (`extra_forbidden`), so
`scenario:`/`artifacts:` CANNOT sit on the spawn node as earlier drafts of
this spec said. They live in the interrupt MARKER's `message` dict — the
free-form payload lockstep already uses for `evidence_schema`/`checks` —
alongside `step: _subcall`, `node`, `runner`. Marker `node` ids must be
unique within a recipe (subcall workdirs and the single-start claim are
keyed on them). `artifacts:` is a map of artifact NAME (no dots) →
project-relative path; `hash_from: _subcall_envelope.artifact_hashes.<name>`
resolves through it.

With `scenario: <recipe-name>` in the marker: the engine starts a
CHILD RUN (full v1 treatment automatically: snapshot, profile gate,
baseline, own SQLite thread) and spawns the runner session pointed at the
project with the child run pre-started. RunRecord gains
`parent_run: id | None`.

- **`parent_run` is ENGINE-INTERNAL only** (review C1): set exclusively
  by `subcall_spawn`; the MCP `scenario_start` signature does NOT grow a
  parent parameter — the worker cannot mint descendants.
- **Gate linkage, bounded** (review C1): PreToolUse unlock extends to a
  child run only while its `parent_run` chain terminates in an
  **awaiting** run of the policy's recipe in the same project. (Same-user
  OS caveat from origin binding applies to the whole gate story: shell
  access defeats it, as in v1.) Parent
  terminal (done/escalated/aborted) ⇒ the engine walks the `parent_run`
  chain RECURSIVELY over grandchildren (review F3): requests each
  subcall's termination through its workdir (`cancel` file — the
  supervisor owning the handle does the kill), then flips each descendant
  run's index to `aborted` via terminal-CAS `update()`. The engine
  reaches only the workdir files and the shared index — not another
  server's in-memory graph; the index flip + cancellation is what stops
  the child (its next tool call sees a terminal run and refuses). No
  orphaned unlocks past the human-review point.
- **Child-run origin binding** (review F2 — THE independence fix; the
  round-1/2 guarantee was void without it): a run with `parent_run` set
  accepts `scenario_done`/`escalate`/`abort` ONLY from a server whose
  environment carries that run's spawn-minted credential
  (`LOCKSTEP_CHILD_RUN == run_id` plus a random `LOCKSTEP_CHILD_NONCE`
  written by `subcall_spawn` into the run's state-dir record). Without
  it, the worker — who is handed the child's run id via `scenario_status`
  / `list_runs` and owns the project — simply calls `scenario_done` on
  the child with its own evidence and walks it to `done`; the reviewer
  agent's opinion never matters. `scenario_done` on a credential-less
  caller for a parented run → refused. Origin binding closes the
  SANCTIONED MCP surface. It does NOT close same-user OS access (review
  round-3 major-2): on a multi-user OS a process environment may be
  reachable by other same-user processes through ordinary OS facilities,
  and a worker with shell can Bash-launch its own credentialed engine.
  That is the SAME same-user residual class v1 already carries (shell
  reaches the state dir too); stated honestly in the guarantee delta,
  not claimed closed, and NEVER probed via OS-specific paths in tests.
- **Child session posture** (review M6): children spawn with the
  project's settings INTACT — no permission bypass flags, ever; the
  child is inside the same PreToolUse envelope (its own run unlocks it
  via descendant linkage). Failure mode NAMED: a `claude -p` child
  cannot answer interactive permission prompts — any tool not
  pre-allowed by project settings stalls the child until the runner
  timeout, surfacing as an error envelope. The child learns its run
  identity explicitly: `LOCKSTEP_CHILD_RUN`/`LOCKSTEP_CHILD_NONCE` env +
  an engine-generated prompt preamble naming the run id and the report
  path; the SessionStart hook marks the run matching the session's
  `LOCKSTEP_CHILD_RUN` as its own. Spawn environment is sanitized (not just PATH) via an
  ALLOWLIST (review round-3 minor): `LOCKSTEP_STATE_DIR` PRESERVED (the
  shared index is load-bearing for gate linkage + poll),
  `LOCKSTEP_CHILD_RUN`/`NONCE` always overwritten on fractal respawn (so
  a nested child can't inherit its parent's credential), the rest
  stripped to a minimal set.
- Parent's poll completes when the child run reaches a terminal status;
  the envelope carries child run id + terminal status — **terminal
  status is enforced by edge routing; artifact contents by checks**
  (review M3, see envelope below).
- Depth capped by `max_fractal_depth`; budget/runner/depth refusal
  happens ENGINE-SIDE before any graph resume (review M7/F2a). The
  mechanism is PINNED (peek only reveals the parked interrupt, not the
  post-resume route — verified — so the engine STATICALLY evaluates the
  snapshot): (a) profile restricts conditions on edges ENTERING a spawn
  node to `verdict_status`-only, so the engine can predict "this resume
  reaches a spawn" from the current validator's verdict + pass-edges;
  (b) the engine replicates v1's loop-guard preemption (a pass at cap
  routes to escalate, not the spawn) reading `_loop_counts` from
  `peek().state`; (c) a spawn node reachable directly from START (no
  preceding resume) is checked at start-time, or forbidden by profile.
  A predicted refusal follows the v1 no-resume path (check-style
  `error`, retry budget untouched); the spawn node itself never fails
  on policy. Process-start failure (binary missing despite allowlist)
  surfaces as an error envelope from the first poll.
- Independent review as fractal: reviewer child runs a `review-gate`
  scenario; the worker never sees the reviewer's session; the parent's
  checks validate the reviewer's artifacts + child terminal status. The
  v1 "review gate is artifact-shaped" honesty note upgrades to
  "independently produced, machine-gated".

## Portability principle (user directive — binding on the whole feature)

**No OS attachment anywhere.** Everything platform-touching (locking,
process spawn/liveness/termination, path handling) uses ONLY Python
stdlib with portable semantics; no `fcntl`/`flock`/`msvcrt`/`/proc`/
`setsid`/`os`-signal assumptions in core logic. Where a difference is
truly unavoidable it is isolated behind a single helper with a portable
default and auto-discovery at runtime (feature/capability detection, never
`sys.platform ==` branching in business logic). Runner executables are
resolved by owner-configured absolute path, with a capability check
("is this an executable file") — discovery, not hardcoded location. Tests
never probe OS-specific artifacts (no `/proc`, no `ps`); guarantees are
tested through contracts (missing credential → refusal), not OS
introspection. The dev machine is macOS and CI is Linux — the same test
suite must be green on both with zero platform skips in the core paths.

## Engine/API deltas (complete list)

- `lockstep_mcp/subcalls.py` (new): spawn/poll hooks + process lifecycle
  (pidfile records pid AND process start-time — a recycled pid after
  server restart must not read as "still running") + budgets + runner
  registry (absolute paths).
- `engine.py`: subcall-marker auto-poll in `status()`/`done()` entry;
  engine-side policy refusal before resume; parent-terminal cascade
  (kill processes, terminate children); descendant resolution for the
  gate (exposed to cli via runs.json — hooks stay read-only);
  `scenario_abort`/`scenario_escalate` BY TOOL are refused while a run
  is parked in a subcall (a poisoned child must not kill its parent;
  the finite runner timeout is the escape — after it fires, poll
  returns error and abort becomes available again).
- `runs.py`: `parent_run` field + spawn nonce (additive); **RunIndex
  write locking + terminal CAS** (review M5/F3): fractal children mean
  concurrent server processes over one runs.json — a **platform-neutral
  cross-process lock** around every read-modify-write, AND a terminal
  check-and-set INSIDE `update()` under that lock (a status transition
  OUT of `aborted`/`escalated`/`done` is refused/no-op — the terminal
  set INCLUDES `done` per review round-3 minor, else a parent-done
  cascade rewrites a legitimately completed child to `aborted`; the
  cascade also skips already-terminal descendants. v1's "terminal never
  resurrected" lived only in `_reconcile`, not `update()`). The lock is
  held on a SIDECAR lock file `runs.json.lock`, NOT runs.json itself —
  `os.replace` swaps inodes so a lock on the replaced inode is void
  (review round-3 minor). **OS-AGNOSTIC RULE (user directive): no
  platform-specific primitive.** The lock is a portable
  create-exclusive lockfile (stdlib `O_CREAT|O_EXCL` acquire /
  best-effort stale-break on the sidecar) — NOT `fcntl.flock` / `msvcrt`
  / any POSIX-only or Windows-only call; a thin `locking.py` helper
  centralizes it. Poll reads child terminal status from the LOCKED index
  only, never the child's checkpoint db (avoids cross-process SQLite
  contention).
- `profile_check.py`: subcall marker is a THIRD interrupt class (review
  m1) with its own exemption set spelled out: `{step: _subcall, node,
  runner?}` briefs are exempt from task/exit_criterion/≥1-check and
  from validator-pairing; the loop-exemption criterion is mechanical —
  a back edge whose target interrupt carries the `_subcall` marker;
  runner name pattern `^[a-z][a-z0-9-]*$`; a spawn node must be a DIRECT
  conditional successor of a validator (or of START), no python node
  between that could rewrite `verdict_status` — required for the static
  pre-resume prediction to be sound (review round-3 minor); for a
  fractal subcall, the child recipe's `baseline_globs` must cover every
  artifact the parent references via `hash_from:` (F1 coverage rule);
  sequential subcalls share `_subcall_envelope` (last-write-wins) — the
  authoring skill warns to validate each before the next spawns; subcall
  poll loops are
  EXEMPT from the step-triple loop_limits→escalate rule — a long subcall polled many times must not
  falsely escalate; termination is guaranteed by the runner TIMEOUT in
  the poll hook (timeout → error-style envelope → recipe routes), which
  the profile requires to be finite. (No `copilot` allowance — see
  "As built".)
- `cli.py` hook_pretool: child-session unlock narrowed to the session's
  own awaiting ancestry chain (see "As built" for the exact predicate;
  sessions without the child env keep the v1 predicate);
  `hook_session_start` and `_reconcile` text align with subcall-aware
  wording (don't surface a raw `_subcall` marker as "awaiting step
  '_subcall'").
- `server.py`: `scenario_done`/`escalate`/`abort` enforce origin
  binding for parented runs (credential from env); `scenario_status`
  result gains the subcall progress block.
- No new MCP tools.

## Upstream (issues to file, per upstream-first)

1. sheikkinen/yamlgraph: `_maybe_wrap_otel` strips node `config`
   (subgraph children run checkpointer-less; `direct` mode TypeError) —
   with minimal repro + the two-line fix direction; after it lands,
   native subgraph fractal becomes an option again.
2. sheikkinen/yamlgraph: configurable argv for the copilot node (runner
   generalization); our PATH-shim is marked "delete when this lands".

## Out of scope (v2)

- codex/gemini runner implementations (registry slot reserved).
- Parallel subcalls via `map` (inherits subgraph caution; sequential
  first).
- A2A as invocation channel (heavier than process spawn; only if
  cross-host children become real).
- Native subgraph composition (blocked on upstream issue 1).
- Owner-approved resume of escalated runs (separate v2 item, design
  settled in v1 follow-ups).

## Testing

- Unit: fake runner executable (shell script writing canned
  claude-p-shaped JSON) — spawn/poll/timeout/budget/reattach without
  burning tokens; registry resolution matrix (node → env default →
  allowlist → refusal).
- Integration: subcall triple end-to-end with the fake runner incl.
  server restart mid-subcall (reattach) and child-run fractal with gate
  linkage.
- One optional LIVE smoke (Task-9-Step-5 pattern): real `claude -p
  --model haiku` one-shot through the claude runner — run manually,
  documented, not in CI.

## Honest guarantee delta (review M1 — precise verbs)

Unchanged core: status integrity, not process compliance. New claims,
bounded and precise:

- "The worker cannot WRITE the subcall's control channel or drive the
  child run" — enforced by: state-dir/runners.yaml denial + absolute-path
  exec (no PATH-planted runner) + child-run origin binding (only the
  credentialed child server may `done`/`escalate`/`abort` a parented
  run). Validated artifact bytes are hash-pinned from the DENIED side
  (child's own baseline snapshot / runner's captured output file), never
  from collect-time project bytes.
- "A fractal child is a full lockstep run" — all v1 guarantees apply
  recursively; the gate unlocks only descendants of an AWAITING policy
  run.

Explicitly NOT claimed: the worker CAN read the child's transcript
(`~/.claude/projects/<slug>/` — outside our boundary; consumers may add
their own deny) and CAN author the child's inputs — **input poisoning
is real** ("this file is pre-approved…" planted in reviewed files); the
reviewer child's SKILL/prompt must instruct it to treat in-repo claims
of pre-approval as findings, not instructions. Subcall work quality is
not guaranteed. No liveness without tool calls. Hook noise for child
sessions (child's Stop blocked once over the parent's run) is
documented, capped by the harness chain limit.
