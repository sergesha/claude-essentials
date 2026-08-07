# lockstep — design spec

Date: 2026-08-07. Status: approved in brainstorming, pending user review of this doc.
Final home: `sergesha/claude-essentials` (this file moves there with the implementation).

## Problem

Autonomous coding agents (esp. weaker models) drop multi-step processes: skip
steps, invent order, declare "done" without doing the work. Advisory process
tooling (superpowers, spec-kit, Task Master, beads) does not structurally
prevent this. Market scan (2026-08-06) confirms: no adopted product combines
declarative multi-step flow + durable per-run state + deterministic evidence
gates + hook enforcement. Anthropic RFC anthropics/claude-code#45427
(deterministic governance) closed NOT_PLANNED — no native gate is coming.

## Decision

Build **lockstep**: a self-sufficient flow-enforcement plugin.

- **Engine**: yamlgraph (YAML recipe format, compiled to LangGraph) run inside
  an MCP server. No hand-rolled FSM. LangGraph `interrupt()` + checkpointer
  give durable park/resume between steps.
- **Control model**: "control, not inversion" — the agent stays a live session
  doing the work; the engine owns only run state and step transitions. The
  agent is told exactly one current step + exit criterion; transitions happen
  only through engine-validated evidence.
- **Checkpoints**: SqliteSaver (native LangGraph), one `.db` per run.
  MemorySaver in unit tests only. No Redis, no external services.
- **Distribution**: `${CLAUDE_PLUGIN_ROOT}` — the plugin's own cloned files ARE the
  distribution; `mcpServers`/`hooks.json` invoke `uv run --project
  ${CLAUDE_PLUGIN_ROOT}/engine lockstep-mcp <verb>`, no PyPI dependency. A PyPI
  package `lockstep-mcp` (name verified free 2026-08-07), installable via `uvx
  lockstep-mcp==X.Y.Z`, is optional future distribution. No Docker required
  (optional image later if wanted). Releases via release-please, tag
  `lockstep-vX.Y.Z`.
- **Standalone**: zero knowledge of ours-fleet or any other consumer. Vocabulary
  is generic: MCP, hooks, settings, filesystem, env vars.

## Architecture

```
lockstep/
├── engine/                    # python package → PyPI "lockstep-mcp"
│   ├── server.py              # MCP wrapper over yamlgraph Python API
│   ├── validators.py          # lockstep_mcp.validators — check registry + run_checks
│   ├── profile_check.py       # lockstep profile on top of `graph lint`
│   └── runs.py                # run index (list_runs) beside checkpoints
├── recipes/examples/          # repo-level example recipes (docs + tests; not packaged)
└── adapters/
    ├── claude/                # v1: plugin.json, hooks.json, skills: lockstep + lockstep-author
    ├── codex/                 # later: config snippet + hook adapter
    └── gemini/                # later: extension manifest + context.md
```

The engine is harness-agnostic (MCP + env only). Adapters are thin. Hooks are
per-harness belt-and-suspenders; the load-bearing enforcement is the engine,
so lockstep degrades gracefully where hooks are unavailable.

MCP server is not a daemon: every tool call loads the run checkpoint,
advances the StateGraph to the next `interrupt()`, replies, exits the turn.
State survives session/server/host restarts.

## MCP tool surface

| Tool | Backing |
|---|---|
| `scenario_start(recipe, vars)` | native (`invoke` + thread_id) |
| `scenario_status(run)` | brief from the run index (`_reconcile` consults the checkpoint); on terminal/done runs returns `{status, recipe, last step}` — no brief |
| `scenario_done(run, step, evidence)` | wrapper `Command(resume=…)` + **our** payload validation (native resume is schema-less) |
| `scenario_escalate(run, reason)` | our semantics on their primitives |
| `scenario_abort(run)` | we build (no native abort) |
| `list_recipes()` | native |
| `validate_recipe(path)` | native `graph validate` + `lint --gate` + our profile |
| `render_flow(recipe, run?)` | native `--mermaid` (+ `--overlay` = taken route) |
| `list_runs()` | we build (own index; sqlite checkpointer exposes no history) |
| `run_trace(run)` | spike-probed; fallback: engine-written transition JSONL |
| `scenario_dryrun(recipe, step, evidence)` | we build — SHAPE checks only (file/content); command and baseline checks report `skipped (dryrun)` — otherwise dryrun of an agent-written recipe is arbitrary command execution bypassing the PreToolUse gate. Project root = server cwd; `_`-rejection and schema validation still apply. README notes: dryrun lets an agent probe tripwire regexes outside the retry budget |

Out of v1: checkpoint time-travel/fork (yamlgraph does not surface it; route
log + overlay cover debugging), token streaming.

## Recipe format (lockstep profile over yamlgraph)

Core convention: **a recipe is a fully deterministic graph — no LLM nodes.**
All intelligence is outside (the agent). Allowed node types: `interrupt`,
`python`, `tool` (shell), `passthrough`, plus expression-based conditional
edges. Forbidden: `llm`, `agent`, `router`, `copilot`, `race` (need API keys,
reintroduce nondeterminism).

A step is a triple: `interrupt → validator → conditional edges
(pass/retry/escalate)`:

Abridged example (only the `plan` step shown; further steps follow the same
triple pattern, chained the way `validate_plan`'s pass edge below would
target `step_implement` instead of `END`). This is copied verbatim in shape
from the spike-frozen fixture (`lockstep/engine/tests/fixtures/recipes/good/
two-steps.yaml`) — not an abridged sketch of a different, easier dialect.
Conventions — ONE dialect, this one: reused state keys
`brief`/`evidence`/`verdict_status`/`verdict_reasons` across all steps
(sequential graphs make reuse safe; one generic validator serves every
step); ALL lockstep extensions (`evidence_schema`, `checks`) live INSIDE
the interrupt `message` dict (a free-form payload — no conflict with
yamlgraph's YAML schema); every interrupt (work step AND `escalate`) needs
`idempotent: false` or it can inherit a stale sibling's payload (both share
`state_key: brief`); conditional edges are `{from, to, condition}` triples,
one condition per edge — there is no `type: conditional` + `conditions:`
list shape in this dialect; `loop_limits`/`loop_exits` key on the
REPEATING node (the python validator), never the interrupt, and
`loop_exits` must target a plain `passthrough` gate — never the `escalate`
interrupt directly, or its `prepare_fn` never runs and the parked brief is
stale instead of the `{step: escalate}` marker (see `yamlgraph_api.py`'s
module docstring for why). The checkpointer is NOT declared in recipes:
the server injects `{type: sqlite, path:
$LOCKSTEP_STATE_DIR/runs/<run-id>.db}` itself.

```yaml
state:
  brief:           dict
  evidence:        dict
  verdict_status:  str
  verdict_reasons: list
  task:            str    # var at scenario_start; substituted into task/exit_criterion ONLY

tools:
  run_checks:
    type: python
    module: lockstep_mcp.validators
    function: run_checks

nodes:
  step_plan:
    type: interrupt
    state_key: brief
    resume_key: evidence
    idempotent: false
    message:
      step: plan
      task: "Составь план работ по {task}"
      exit_criterion: "план в .lockstep/plan.md; каждый пункт с файлом и проверкой"
      evidence_schema:
        required: [plan_path]
        properties:
          plan_path: {type: string, format: project-path}   # relative, resolved+contained by server
      checks:
        - type: md_has_sections
          path_from: plan_path
          sections: [Files, Steps, Checks]

  validate_plan:
    type: python
    tool: run_checks    # republishes the engine-embedded verdict to state (engine ran the checks)

  escalate_gate:
    type: passthrough
    output: {}

  escalate:
    type: interrupt
    state_key: brief
    resume_key: evidence
    idempotent: false
    message:
      step: escalate     # marker: engine flips run to terminal 'escalated' here

edges:
  - from: START
    to: step_plan
  - from: step_plan
    to: validate_plan
  - from: validate_plan
    to: END               # next step's interrupt in a real multi-step recipe
    condition: "verdict_status == 'pass'"
  - from: validate_plan
    to: step_plan
    condition: "verdict_status == 'fail'"
  - from: escalate_gate
    to: escalate

loop_limits:
  validate_plan: 2    # "2 allowed executions" — every fixture uses 2
loop_exits:
  validate_plan: escalate_gate
```

(Verdict shape is frozen FLAT — `verdict_status` / `verdict_reasons` state
keys — so edge conditions never depend on dotted dict access support.)

Validation vocabulary (recipe authors write zero code): the declarative
`checks` list in each step's message, executed ONCE by the ENGINE at
`scenario_done` (the in-graph `run_checks` node only republishes the
engine's verdict — see the scenario_done path below):

- shape: `file_exists`, `file_nonempty`, `md_has_sections`, `file_matches`
  (regex content assertion — a review gate checks `Verdict:\s*PASS`; honest
  label: a TRIPWIRE against lazy hollowness, not a guarantee — the agent
  writes to the regex);
- commands: `cmd_ok` (LITERAL command pinned in the recipe, default `cwd` =
  the run's project root, default timeout 600s), `git_clean`,
  `junit_gate {command, min_tests, max_skipped?}` (runs the pinned command
  with `--junitxml` written into the STATE dir — unforgeable by the agent —
  and asserts collected/failures/skips; closes test-weakening and
  skip-gaming; note `pytest -q` with zero collected already exits 5);
- baseline (backed by the run's baseline manifest, below): `fresh
  {path_from}` — artifact bytes produced during THIS run; `unchanged {glob}`
  — e.g. `tests/**` byte-identical to baseline, re-hashed immediately after
  any `cmd_ok` or `junit_gate` in the same pass (kills test-weakening by
  construction);
  `changed_in {paths}` — work happened in the declared area; `diff_only
  {paths}` — the step's diff (vs previous step's snapshot) touches ONLY
  declared paths — scope containment on the passable state.

Check CRASH ≠ check FAIL: a check that raises (missing binary, timeout)
yields verdict `error`, reported distinctly, and does NOT consume the retry
budget — validator brittleness must not convert into terminal escalation.

Escape hatch: recipe-local `tools.py` python nodes — last resort,
`validate_recipe` flags it (warn, requires human review).

**Baseline manifest.** At `scenario_start` the engine hashes
recipe-declared `baseline_globs` (resolved against `run.project`;
`{path: sha256}`; default ignore set
`__pycache__/`, `*.pyc`, `.git/` — else a pytest run dirties its own
`unchanged` gate; symlinks not followed) into TWO artifacts: the immutable
run-start manifest `runs/<run-id>.baseline.json` and per-step snapshots
`runs/<run-id>.baseline.<n>.json` written on each PASS. `unchanged` and
`changed_in` take `since: start|previous` (default `start` — "byte-identical
to RUN-START", so an early step can't quietly re-bless a weakened tree);
`diff_only` compares to `previous`; `baseline.0.json` is written at start so
step 1's `previous` has a referent; the final PASS also snapshots
(harmless, specified). `unchanged` re-hashes after any `cmd_ok` OR
`junit_gate` in the same pass. **Coverage is enforced, not assumed:** a
baseline check whose target path is NOT covered by `baseline_globs` returns
`error` (never a vacuous pass — else `fresh` silently blesses stale
out-of-glob artifacts). Honest scope: `diff_only` containment is "within
the declared globs", not the whole filesystem — and recipes gating on test
runs must include the runner's CONFIG surface in the globs (root
`conftest.py`, `pytest.ini`, `pyproject.toml`), or an agent reconfigures
the runner outside the fence. The agent cannot re-baseline (state dir
denied).

`validate_recipe` = native gate + lockstep profile: no LLM nodes; EVERY
outgoing edge of a work-interrupt targets its validator node (bypass edges
are errors, not just "a validator exists"); step briefs carry
`step/task/exit_criterion` and ≥1 check; every retry loop capped by
`loop_limits` with `loop_exits` targeting an interrupt carrying the
`{step: escalate}` marker (marked nodes are exempt from the validator-pairing
rule); no `checkpointer:` block (server-injected); `command_from` or any
command-from-evidence shape is an error; any `{...}` placeholder inside
`checks` or `evidence_schema` is an error (vars substitute ONLY into
`task`/`exit_criterion` — checks and schemas are used VERBATIM from the
snapshot, else agent-supplied vars rewrite "pinned" commands/regexes);
every `path_from` key used by checks must be schema-annotated
`format: project-path` (unannotated paths would bypass containment). A
recipe failing the profile does not start.

**Recipe snapshot (tamper + drift guard).** At `scenario_start` the server
copies the recipe bytes into `$LOCKSTEP_STATE_DIR/runs/<run-id>.recipe.yaml`
and compiles ONLY from that snapshot for the run's lifetime. The live file
in `<project>/.lockstep/recipes/` is agent-writable by definition; without
the snapshot, a mid-run edit silently swaps the policy the run is judged by
(and even benign edits desync graph structure from the checkpoint). Profile
validation runs on the snapshot. README additionally advises consumers to
deny agent writes to `.lockstep/recipes/**`.

Subagents (e.g. code review): dispatched by the agent, not the engine. The
brief instructs "get an independent review, report at `.lockstep/review.md`";
the exit criterion checks the artifact. Engine-side agent invocation
(yamlgraph `copilot`-node style) is explicitly rejected — that is inversion
again, plus keys inside the engine.

## Recipe authoring (instruction skill, v1)

The plugin ships a second skill, `lockstep-author` (name provisional): a
plain instruction manual for writing recipes — the recipe dialect crib (the
step triple, brief/evidence/verdict conventions, loop_limits/loop_exits),
the full check-type vocabulary with arguments, the tools.py last-resort
policy, and the working ritual: interview the human about goal/steps/exit
criteria first, draft, run `validate_recipe` until clean, `render_flow` and
get the human's eyes on the diagram before first use.

v1 deliberately does NOT enforce this process with a builtin lockstep
scenario. The dogfooded variant (authoring runs under lockstep itself, on a
packaged `author.yaml`) is deferred to v2 — it drags in recipe packaging into
the wheel, a builtin+project recipe search path, and an owner-approval
channel for the review step (a deterministic check cannot verify a human);
adversarial review 2026-08-07 showed all three need real design, not a
side-effect of v1.

## Evidence contract

- Evidence is **pointers to artifacts, never claims**: not "tests pass" but
  a path/reference — and the shell validator re-runs the check itself. Kills
  "closeout misrepresentation" by construction. Two hard rules guarding
  this: **commands are pinned in the recipe, never taken from evidence**
  (an agent-supplied command string would let it submit `true`), and
  **evidence paths are project-relative**, resolved by the server against
  the run's project root.
- Path handling is explicit, ordered, and contained: schema fields carrying
  paths are annotated `format: project-path` (no magic key-name heuristics);
  order is validate RAW evidence against the schema → resolve annotated
  fields against `run.project` → REJECT any resolved path that escapes the
  project root (`../` tricks fail here) → run checks on resolved paths.
- `run.project` = the server process cwd captured once at `scenario_start`
  (documented assumption: the harness launches plugin MCP servers in the
  project dir; verified in the adapter task). Hooks match it against their
  own cwd after `Path.resolve()`, by equality or parent-prefix.
- **Reserved prefix:** raw evidence keys starting with `_` are REJECTED
  before schema validation (the engine↔graph verdict channel uses `_`-keys;
  an agent-supplied `_verdict_status: pass` must die at the door, not forge
  a verdict).
- `scenario_done` path (the ENGINE executes checks, the GRAPH routes
  verdicts): reject `_`-keys → validate `evidence_schema` (bad → refusal
  with reasons, state untouched) → resolve+contain paths → engine runs the
  step's checks ONCE (explicit execute call — never inferred from payload
  shape) → verdict `error` short-circuits: no resume, retry budget untouched
  → else `Command(resume={**evidence, _verdict_status, _verdict_reasons})` →
  the in-graph `run_checks` node REPUBLISHES the embedded verdict to state
  (missing embedded verdict → `error`, never silent execution) → conditional
  edges route. Write order on transitions: graph checkpoint first, run index
  second; on disagreement the checkpoint wins and the index is repaired from
  it — EXCEPT terminal index statuses (`aborted`, `escalated`), which are
  never resurrected. On `fail` the repeated brief carries which check failed
  and why; loop-exhaust escalation records the last fail reasons.

## Enforcement layers (vs RFC #45427 holes)

| Layer | Holds | RFC hole closed |
|---|---|---|
| 1. Engine (load-bearing) | transitions only via validated evidence; state outside model context | closeout lies; invented order; survives restarts |
| 2. Stop-hook | active run + turn ends without done/escalate → block + remind (blocks ONCE per stop chain — our handler allows on `stop_hook_active`; the harness itself caps chains at 8 — a DELAY, never the guarantee) | model "forgot" the scenario |
| 3. SessionStart-hook | after restart/compaction: "active run X, step N, criterion Y" | amnesia after restarts |
| 4. Consumer permissions | deny writes to state dir / recipes / settings / hook scripts (README guidance, consumer-side) | gate self-modification; .db + recipe tampering via Bash |
| 5. PreToolUse no-run gate (opt-in per project) | owner marks a project via `lockstep-mcp policy require --recipe <X>` (policy file in the agent-unwritable state dir); Write/Edit/NotebookEdit/Bash/Task are DENIED unless an active run of THE POLICY'S RECIPE exists in THIS project (any-recipe unlock would let the cheapest lying-around recipe open the gate); no Bash parsing, so heredoc bypasses are moot (Read/Grep stay open) | "agent never starts a run / works outside the mechanism" — in policy-marked projects, the path of least resistance IS `scenario_start` of the required recipe |
| 6. Observability | `list_runs` shows stalled runs; `lockstep-mcp doctor` checks dirs + installed version, and fails loudly on an active run with no binding sidecar — the signature of a PostToolUse matcher that does not match this install's tool names (handler self-exec is still v2). Hook death is silent — nothing observes hook liveness; the engine's evidence gate is the load-bearing layer and does not depend on hooks. Known caveat: write-capable MCP tools from OTHER servers are outside the PreToolUse matcher — README lists it | silent ignoring |

Hook handler discipline: only PreToolUse is internally fail-closed (it is
the one hook that can actually block an action — any internal exception
still yields an explicit `deny`, never an unhandled exit the harness would
treat as fail-open); "no policy file → allow", so a broken install bricks
only opted-in projects, where bricking closed is the point. Stop and
SessionStart are fail-OPEN by design: they can only delay/annotate a turn,
never truly enforce, and this advisory layer must not brick a project on
its own internal error — an exception in either yields the same "nothing
to report" result as having nothing configured.

Residual risks (documented in README): subagent hook bypass is irrelevant
(enforcement is engine-side); hooks kept trivial + tested because silent hook
failure is real; outside owner-managed permission setups layer 4 is only
filesystem rights.

**The honest guarantee (README headline — do not overclaim):** a lockstep
run cannot report progress without deterministically validated,
artifact-backed evidence — run status is machine-checked, never
self-attested — PROVIDED the consumer denies the agent writes to the state
dir and the recipe sources. With baseline checks in the recipe this
extends to: artifacts were produced during the run (`fresh`), the test
tree the pinned command ran against is byte-identical to baseline
(`unchanged`), and a step cannot PASS while its diff touches undeclared
paths (`diff_only`). In policy-marked projects the PreToolUse gate adds:
no writes at all outside an active run, while hooks fire. What is NOT
guaranteed: work *quality* (shape checks are tripwires), and anything
hook-borne — a hook that dies, dies silently; nothing observes hook
liveness, and the load-bearing evidence gate lives in the engine, not
the hooks. Guarantee strength is "integrity of the
status signal", not "process compliance".

## Packaging & config

- Config via env only: `LOCKSTEP_STATE_DIR` (default `~/.lockstep`, outside
  the project — not in git, easy to deny), `LOCKSTEP_RECIPES` (default
  `<project>/.lockstep/recipes`). Runs: `$LOCKSTEP_STATE_DIR/runs/<run-id>.db`
  + `runs.json` index. Evidence artifacts live in the project (`.lockstep/`).
- Pins: langgraph + yamlgraph pinned in the package; dependency bumps are
  plugin releases, never silent.
- yamlgraph risk: single author, fast pace, no documented schema-versioning —
  hard pin + our own recipe-compilation tests in CI.

## Testing

- Unit: MemorySaver runs of fixture recipes; profile_check on a corpus of
  good/broken recipes; evidence schema accept/reject; example recipes pass
  `validate_recipe`.
- Integration: uvx-installed server + SQLite + fake agent (script driving the
  tools) — full cycle start→done→retry→escalate→server-restart→resume
  (durability proof).
- Hook tests: Stop-hook blocks/passes by exit code.

## Out of scope (v1)

- **Dogfooded authoring scenario**: packaged `author.yaml` + builtin recipe
  search path in the wheel + owner-approval channel (e.g.
  `lockstep-mcp approve <run>` nonce) — v2, designed together.
- codex/gemini adapters (structure reserved, thin follow-ups).
- Time-travel/fork, streaming.
- Resumable escalation: in v1 `escalated` is TERMINAL. v2 design SETTLED
  (2026-08-07): `lockstep-mcp approve <run>` run by the owner writes a
  nonce file into the agent-unwritable state dir; agent-callable
  `scenario_resume(run)` requires + consumes it (file existence in the
  denied dir IS the authentication — no new trust channel);
  `seed_from=<run>` restart sugar gated by the same file.
- v2 hardening backlog (designs sketched in plan follow-ups): per-step
  PreToolUse path allowlists (3b); cross-step consistency via
  `from_state:` check args (1d); local venv for sub-50ms hook fires.
- Any fleet integration. Consumer work for the ours-fleet hosts (lockstep
  profile: permissions allow/deny sets, skill wiring, molecule coverage)
  is a separate effort in the VPS/Hetzner repo after the plugin exists.
- Canvas/visual editing (mermaid render covers visualization for now).

## Market context (2026-08-06 scan)

Closest existing: TDD Guard/Probity (hook enforcement, fixed rule set — not a
general FSM); Task Master/beads (durable tasks, advisory); spec-kit/OpenSpec/
superpowers (process artifacts, advisory); a cluster of 0-star solo repos
(GMDW et al.) reinventing exactly this combination — pattern validated,
niche unconsolidated.
