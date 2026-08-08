---
name: lockstep-author
description: Use when authoring or editing a lockstep recipe (a .lockstep/recipes/*.yaml flow definition) — the dialect, the check vocabulary, and the validation ritual before a recipe is trusted with a real run
---

# lockstep-author

This is an **instruction manual, not a builtin scenario** — there is no `scenario_start`
recipe for "author a recipe" in v1. You write the YAML yourself, using this skill as the
dialect reference and vocabulary table, then validate it with the `validate_recipe` /
`scenario_dryrun` / `render_flow` MCP tools before anyone runs it for real.

## The ritual

1. **Interview the human** before drafting anything: what is the goal, what are the discrete
   steps (each step becomes one interrupt→validator pair), what counts as "done" for each step
   (the `exit_criterion` a human would accept), and what should happen when a step keeps
   failing (the escalation policy — who gets pinged, is there a fallback path). Do not guess
   these; a recipe encodes a real workflow contract and should read back as one the human
   recognizes.
2. **Draft the recipe** using the dialect below. Start from
   `lockstep/recipes/examples/feature-dev.yaml` — copy it into `<project>/.lockstep/recipes/`
   and adapt the steps, `evidence_schema`, and `checks` rather than writing the yamlgraph
   scaffolding (interrupt/validator/passthrough/escalate wiring) from scratch.
3. **`validate_recipe(path)`** until clean. It runs two independent checks and reports both:
   `yamlgraph.ok` (does it compile as a yamlgraph graph at all) and `errors`/`warnings` (does
   it satisfy the lockstep profile — the structural rules in this skill's dialect crib). Fix
   every error; read every warning (a warning is the profile telling you something is
   unusual, not necessarily wrong — see `tools.py policy` below for the one warning type that
   exists today).
4. **`scenario_dryrun(recipe, step, evidence)`** against each step with plausible evidence.
   This runs the step's **shape checks only** (`file_exists`, `file_nonempty`,
   `md_has_sections`, `file_matches`) against whatever evidence you hand it — command checks
   (`cmd_ok`, `junit_gate`, `git_clean`) and baseline checks (`fresh`, `unchanged`,
   `changed_in`, `diff_only`) report `skipped (dryrun)` and never execute. It is a shape-only
   smoke test, not a full rehearsal — it exists so an agent-authored recipe can be probed
   without arbitrary command execution bypassing the PreToolUse gate (see "what dryrun does
   NOT do" below).
5. **`render_flow(recipe)`** and put the mermaid output in front of the human before the first
   real run. A flow diagram catches wiring mistakes (a step that can never be reached, a loop
   that never exits) that pass validation but are still wrong.

## Recipe dialect crib

The dialect below is **pinned** to yamlgraph 0.5.18 — copied verbatim from
`lockstep/engine/tests/fixtures/recipes/good/two-steps.yaml` and
`lockstep/recipes/examples/feature-dev.yaml`, the two real fixtures the engine's own tests
compile and run. Do not improvise a different shape for any of the traps below: each one is a
live failure mode, not a style preference.

**Every step is a triple**: an `interrupt` node (the agent-facing step) → an unconditional edge
→ a `python` validator node (`tool: run_checks`, wired once via `tools: { run_checks: {type:
python, module: lockstep_mcp.validators, function: run_checks} }`) → two **conditional edges**
back out of the validator:

```yaml
nodes:
  plan_step:
    type: interrupt
    state_key: brief
    resume_key: evidence
    idempotent: false          # TRAP: every interrupt needs this, see below
    message:
      step: plan
      task: "..."
      exit_criterion: "..."
      evidence_schema: { required: [plan_path], properties: { plan_path: { type: string, format: project-path } } }
      checks:
        - type: md_has_sections
          path_from: plan_path
          sections: [Approach, Steps]

  validate_plan:
    type: python
    tool: run_checks

edges:
  - from: plan_step
    to: validate_plan
  - from: validate_plan
    to: implement_step
    condition: "verdict_status == 'pass'"
  - from: validate_plan
    to: plan_step
    condition: "verdict_status == 'fail'"
```

**Trap — conditional edges are `{from, to, condition}` triples, one condition per edge.** There
is no `type: conditional` node with a `conditions:` list — that shape does not exist in this
dialect and the profile flags it as an invalid edge shape rather than silently accepting it.
Always write two separate edges (pass / fail) out of a validator, each with its own `condition`.

**Trap — work-interrupt `step` names must be UNIQUE across the recipe.** The engine keys
`scenario_done(run_id, step, ...)` and its subcall spawn prediction on the parked step name; two
work interrupts sharing a `step` would make it read the wrong validator. The profile refuses the
recipe (`duplicate step name`). The `escalate` and `_subcall` markers share their step by
construction and are exempt.

**Trap — `idempotent: false` on EVERY interrupt, no exceptions.** All interrupts in one recipe
share `state_key: brief`. The default (`idempotent: true`) reuses whichever payload parked
first at that state key across ANY interrupt sharing it — so a work step without
`idempotent: false` can re-present a stale brief instead of its own. This applies to the
`escalate` interrupt too.

**Trap — `loop_limits`/`loop_exits` are keyed on the REPEATING node, never the interrupt.** The
repeating node is the python validator (e.g. `validate_plan`), not the interrupt it loops back
to (`plan_step`). Interrupts don't consult `loop_limit` at all — only the validator node that
the loop actually re-enters does:

```yaml
loop_limits:
  validate_plan: 2       # "2 allowed executions" — the fixtures use 2 throughout
loop_exits:
  validate_plan: escalate_gate   # NOT the escalate interrupt directly — see next trap
```

Semantics are "N allowed executions of that node", not "N retries" — a limit of 2 allows two
real validator executions and blocks the third. Copy the fixtures' `2` rather than deriving your
own number unless you have a specific reason to diverge.

**Trap — `loop_exits` must target a `passthrough` gate, never the `escalate` interrupt
directly.** yamlgraph's loop-exit routing skips the interrupt's own `prepare_fn` when jumping
straight to an interrupt, so a direct `loop_exits: {validate_plan: escalate}` parks on the
`escalate` interrupt with a **stale `brief`** left over from the last real step, instead of the
`{step: escalate}` marker the engine looks for. Always route through an intermediate
`passthrough` node with exactly one outgoing edge to the (authored, non-loop-exit) `escalate`
interrupt — the profile enforces this shape and will refuse a recipe that skips it:

```yaml
nodes:
  escalate_gate:
    type: passthrough
    output: {}
  escalate:
    type: interrupt
    state_key: brief
    resume_key: evidence
    idempotent: false
    message:
      step: escalate     # exactly {step: escalate} (+ optional text) — the escalate marker
edges:
  - from: escalate_gate
    to: escalate
```

All steps in one recipe can and should share the same `escalate_gate`/`escalate` pair — see
`feature-dev.yaml`, where four validators all point `loop_exits` at the same gate.

**Forbidden node types**: `llm`, `agent`, `router`, `copilot`, `race` — anywhere in `nodes:`.
Lockstep recipes are deterministic graphs; anything that puts an LLM call or nondeterministic
routing inside the recipe itself defeats the whole point (the agent works OUTSIDE the graph,
between `scenario_status` and `scenario_done` calls — the graph only ever sees interrupts and
python validators).

**No `checkpointer:` block.** The engine owns persistence entirely; a recipe that declares one
is rejected.

**`command_from` is forbidden anywhere in a check config.** Commands are pinned literally in
the recipe (`command: "pytest -q"`), never taken from evidence — an evidence-sourced command
is arbitrary code execution by construction.

**`command` runs via `shlex.split()`, with no shell — no pipes, `&&`, `||`, `>`, `$(...)`, or
env-var expansion.** The engine runs `subprocess.run(shlex.split(command), ...)` directly, never
through `/bin/sh -c`. A command like `"pytest -q | tee out.log"` does NOT pipe — it runs `pytest`
with literal arguments `-q`, `|`, `tee`, `out.log`, which `pytest` rejects as bad CLI args (an
ordinary `fail`, not a crash, but not what it looks like either). Write single commands (or a
wrapper script checked into the project and invoked by path) instead of shell pipelines.

**Placeholders (`{var}`) are WHITELISTED to `task`/`exit_criterion` only.** `scenario_start`'s
`vars` substitute into those two fields; `checks` and `evidence_schema` are used **verbatim**
from the recipe snapshot, on purpose — a `vars: {module: "x || true"}` against a pinned
`pytest tests/{module}` would otherwise be a command-injection path. The profile detects and
refuses any string in `checks`/`evidence_schema` matching `\{[A-Za-z_]\w*\}` (letters/underscore
only — `\d{3}`-style regex quantifiers and JSON-schema `pattern` braces don't collide with this
pattern, since a bare digit isn't `[A-Za-z_]`). **Residual collision**: a Unicode
property-escape token like `\p{L}` DOES collide — `{L}` alone matches the placeholder pattern
(`L` is a letter), so a `file_matches` regex or JSON-schema `pattern` using `\p{...}` will be
refused at `validate_recipe` time as a false-positive "placeholder found". This is **loud-only**
— it never silently strips or mis-substitutes anything, checks are never touched by
substitution regardless of what triggers this — but it does mean `\p{...}`-style escapes are
effectively unusable in this dialect today. (Separately: Python's standard `re` module, which
`file_matches` uses, doesn't support `\p{...}` at all — it would raise at check-execution time
even past the profile, so avoid this construct entirely rather than working around the
false-positive.)

**Every `path_from: key` check needs its evidence key annotated `format: project-path`** in
that step's `evidence_schema.properties`. This is how the engine knows which evidence values to
resolve-and-contain against the project root before any check touches them; an unannotated
`path_from` key is a profile error, not a runtime surprise.

**Baseline checks need `baseline_globs` declared** — see the vocabulary table below; a recipe
using `fresh`/`unchanged`/`changed_in`/`diff_only` without a top-level `baseline_globs:` list
is a profile error (else the check would fail at runtime forever, never a silent pass).

## Check vocabulary (all of it — an undocumented check reads to the agent as absent)

Args marked **(evidence-relative)** must be annotated `format: project-path` on that evidence
key in the step's `evidence_schema`; args marked **(recipe-pinned)** are literal values in the
recipe, never sourced from evidence.

| Type | Args | What it does | Caveats |
|---|---|---|---|
| `file_exists` | `path` (recipe-pinned) **or** `path_from` (evidence-relative) | File exists at the resolved path. | Shape check — a tripwire, not proof of content quality. |
| `file_nonempty` | `path` **or** `path_from` | File exists and has non-zero size. | Same tripwire caveat — an empty-but-present file fails; a one-byte junk file passes. |
| `md_has_sections` | `path` **or** `path_from`; `sections: [str, ...]` | Each name in `sections` must appear as a markdown heading (`#`–`######`, case-insensitive) somewhere in the file. | Tripwire: proves a heading with that text exists, not that the section says anything meaningful. |
| `file_matches` | `path` **or** `path_from`; `regex` (recipe-pinned, Python `re.search` pattern against the whole file text) | Regex must match somewhere in the file. | **Tripwire caveat**: this is a substring pattern match, trivially satisfiable by an agent that writes the matching text without doing the underlying work (e.g. `"Verdict: PASS"` at the end of a review file it wrote itself) — it proves the artifact SAYS the thing, not that the thing is true. Don't use it as the only gate on anything load-bearing. Avoid `\p{...}` Unicode escapes (see the placeholder-collision note above — plain Python `re` doesn't support them and they also collide with the placeholder detector). |
| `cmd_ok` | `command` (recipe-pinned, literal string — **`command_from` is forbidden**); optional `cwd`, `timeout` (default 600s) | Runs the pinned command; passes iff exit code is 0. | No output inspection at all — a command that exits 0 while doing nothing useful passes. |
| `junit_gate` | `command` (recipe-pinned); `min_tests` (required int); `max_skipped` (optional int); optional `cwd`, `timeout` | Runs `command` with `--junitxml=<tmpfile>` appended, parses the resulting JUnit XML, and fails if `tests < min_tests`, or any `failures`/`errors`, or `skipped > max_skipped` (when set). | **Pytest-oriented caveat**: the engine literally appends `--junitxml=<path>` as an extra CLI argument to whatever `command` you pinned — the command MUST be `pytest`-compatible (or another runner that accepts an identical `--junitxml=PATH` flag and emits the same `testsuite`/`testsuites` XML attributes: `tests`, `failures`, `errors`, `skipped`). A runner that doesn't understand that flag produces no XML file, which the check reports as an ordinary `fail` ("no junit xml produced") — not a crash, but also not what a naive reader might expect from "run the test suite". |
| `git_clean` | optional `cwd`, `timeout` | Runs `git status --porcelain` in the resolved cwd; passes iff output is empty. | Fails (not errors) if `git status` itself errors, e.g. not a git repo. |
| `fresh` | `path` **or** `path_from` | File exists AND its hash differs from the run-**start** baseline (or is new since start). | **Always compares against run start — there is no `since:` option for `fresh`.** (Contrast `unchanged`/`changed_in`, which do take `since:`.) Target must be covered by `baseline_globs` or the check raises (→ `error` verdict, not a vacuous pass). |
| `unchanged` | `glob` (fnmatch-style pattern, e.g. `"tests/**"`); `since: start\|previous` (default `start`) | No file matching `glob` differs from the selected baseline snapshot. | Deferred to the END of the check pass regardless of list position, and re-hashes AFTER every `cmd_ok`/`junit_gate` in the same pass (TOCTOU guard — a command earlier in the list that mutates a "frozen" file is still caught). **Coverage rule differs from the others**: `glob` must appear **verbatim** as an entry in `baseline_globs` (or match existing manifest entries) or the check raises — not just prefix-covered. `since: start` treats "absent at start and absent now" as pass (a project with no `pytest.ini` stays clean; *creating* one mid-run counts as a change → fail). |
| `changed_in` | `paths: [str, ...]`; `since: start\|previous` (default `start`) | At least one file under any of `paths` differs from the selected baseline snapshot. | Each declared path must be covered by `baseline_globs` (prefix-match) or the check raises. |
| `diff_only` | `paths: [str, ...]` | No file OUTSIDE `paths` differs from the **previous**-step baseline snapshot. | **Always compares against the previous step's snapshot — there is no `since:` option for `diff_only`, it is hardwired to `previous`.** Each declared path must be covered by `baseline_globs` or the check raises. Use this on the LAST step too, not just implementation steps — a recipe that only fences intermediate steps lets post-gate weakening of tests/config slip through clean at the very end (see `feature-dev.yaml`'s review step). |
| `file_matches_hash` | `path_from` (evidence-relative); `hash_from` (recipe-pinned string, must match `_subcall_envelope.artifact_hashes.<name>`) | File's SHA-256 must equal the hash pinned at `hash_from` in graph state. | **Fractal subcalls only** — `hash_from` resolves against the child run's own validated baseline snapshot, never against collect-time project bytes. **Be precise about what that buys**: the pin proves the bytes are unchanged **since the child run's last validated PASS** — the worker can author them up to that instant, so the pin is provenance, not content. Always pair it with a content check on the same file (the shipped example adds `file_matches` on `Verdict:\s*PASS` — without it a FAIL verdict passes the gate). A one-shot subcall (no `scenario:` on the marker) never populates `artifact_hashes` — it validates the **envelope** instead (`output`/`exit_code`/`session_id`), not a project file. `hash_from` naming an artifact no marker declares in `artifacts:` is a profile error. |

**`baseline_globs`** (top-level recipe key, required whenever any baseline check above is used):
a list of glob patterns (supports `**`, resolved via Python's recursive glob) defining which
files the engine hashes into baseline manifests. Manifests are captured at run start
(immutable) and again after every step that PASSes — `since: previous` compares against the
latest passed-step snapshot, `since: start` (and `fresh`, always) compares against the
run-start snapshot. Default-ignored regardless of glob: `__pycache__/`, `.git/`, `*.pyc`;
symlinks are never followed. **Globs alone gate nothing** — only the checks above that actually
read a baseline enforce anything; declaring `baseline_globs` without a corresponding
`unchanged`/`changed_in`/`diff_only`/`fresh` check is a no-op. If a step gates on a test run
(`junit_gate`), the runner's **config surface** — `conftest.py`, `pytest.ini`,
`pyproject.toml`, or whatever your runner reads — must be in `baseline_globs` too, or an agent
can silently reconfigure the runner instead of fixing the code; see `feature-dev.yaml`'s
`test_step` for the pattern (`unchanged` on the test tree AND every config file, `since: start`).

**Check-crash semantics** (relevant when you're debugging a recipe, not something you author):
a check that raises (missing baseline, uncovered path, `re.error`, etc.) yields verdict
`error`, reported separately from an ordinary `fail`, and does **not** consume the step's
`loop_limits` budget — the graph never resumes on an `error`. This is why the coverage rules
above raise instead of silently passing or failing: an `error` is loud and free, a wrong `pass`
would be a real integrity hole.

## Subcalls (v2) — spawning an independent CLI-agent session

A **subcall** is a recipe-level triple built from existing node types only —
a `python` spawn node, an `interrupt` marker node, a `python` poll node —
that hands one closed sub-task to a separate `claude -p` process the main
(worker) agent cannot read or steer. `subcall-one-shot.yaml` (one-shot,
validated by its captured output) and `subcall-fractal.yaml` +
`child-review.yaml` (fractal — the spawned session runs its own lockstep
child run) in `tests/fixtures/recipes/good/` are the ground truth; copy
their shape rather than improvising. `recipes/examples/feature-dev-reviewed.yaml`
+ `review-gate.yaml` are a full worked example of the fractal form.

**The triple:**

```yaml
tools:
  subcall_spawn: {type: python, module: lockstep_mcp.subcalls, function: spawn}
  subcall_poll:  {type: python, module: lockstep_mcp.subcalls, function: poll}

nodes:
  review_spawn: {type: python, tool: subcall_spawn}
  review_wait:
    type: interrupt
    idempotent: false
    state_key: brief
    resume_key: evidence
    message:
      step: _subcall            # the marker discriminator — exactly this
      node: review              # unique node id within the recipe
      runner: claude
      timeout_minutes: 30
      prompt: "..."             # verbatim, no {var} placeholders
      scenario: review-gate     # OMIT for a one-shot subcall
      artifacts: {review: ".lockstep/review.md"}   # fractal only
  review_poll: {type: python, tool: subcall_poll}

edges:
  - {from: validate_plan, to: review_spawn, condition: "verdict_status == 'pass'"}
  - {from: review_spawn, to: review_wait}
  - {from: review_wait, to: review_poll}
  - {from: review_poll, to: review_wait, condition: "_subcall_status == 'running'"}
  - {from: review_poll, to: <next>, condition: "_subcall_status == 'done'"}
  - {from: review_poll, to: escalate_gate, condition: "_subcall_status == 'error'"}
```

**Trap — `scenario:`/`artifacts:` live in the marker's `message`, never on
the spawn node.** yamlgraph 0.5.18 rejects unknown keys in a node's config
(`extra_forbidden`), so the fractal-child wiring rides the marker's
free-form `message` dict alongside `step`/`node`/`runner`/`prompt`/
`timeout_minutes` — the same place `evidence_schema`/`checks` already live
on work interrupts. Putting `scenario:` on `review_spawn` instead compiles
to nothing and the profile will not catch it there — it isn't a recognized
key on either node type.

**Declare the subcall state channels.** Any recipe using subcalls must add
`_subcall_status: str` and `_subcall_envelope: dict` to `state:` — LangGraph
drops undeclared channels, and the profile refuses a subcall recipe missing
either.

**Profile traps specific to the triple** (each is a `check_recipe` error,
not a runtime surprise):

- Every edge **entering** a spawn node must be conditioned exactly
  `verdict_status == 'pass'` or `verdict_status == 'fail'` — nothing else,
  and never from `START` (a start-time spawn would fire on an empty
  evidence channel and bypass the engine's done()-time policy prediction
  for runner/budget/depth). A spawn must be the direct conditional
  successor of a validator.
- Every edge **out of a poll node** must be conditioned exactly
  `_subcall_status == 'running'`, `'done'`, or `'error'` — the `'running'`
  back edge to the marker is required.
  Poll-loop back edges are exempt from `loop_limits`/`loop_exits` — a long
  subcall polled many times must not falsely escalate; termination is the
  runner's own `timeout_minutes`, which must be a finite positive integer
  (an unbounded/`0`/negative timeout is a profile error).
- `runner:` (when given) must match `^[a-z][a-z0-9-]*$` — a name looked up
  in the owner's `runners.yaml`, never a command. `node` (the marker id)
  must match `^[a-z][a-z0-9_-]*$` and be unique within the recipe (subcall
  workdirs and the single-start claim are keyed on it).
- `prompt` is required, non-empty, and used **verbatim** — the placeholder
  scan (`\{[A-Za-z_]\w*\}`) applies to it exactly as it does to
  `checks`/`evidence_schema`; a subcall prompt is never a template.
- **Sequential subcalls share `_subcall_envelope`** (last-write-wins) — if
  a recipe spawns a second subcall after the first, validate/consume the
  first's envelope (via a check with `hash_from`, or a validator step)
  before the second spawn resumes, or its result is gone.
- **Fractal children need covering `baseline_globs`.** Every artifact a
  marker names in `artifacts:` must be a path the CHILD recipe's own
  `baseline_globs` covers — that's where the parent's `hash_from:
  _subcall_envelope.artifact_hashes.<name>` gets its hash from (the
  child's last validated baseline snapshot). An uncovered artifact is a
  profile error at `validate_recipe`/`check_recipe` time, not a runtime
  surprise — `check_recipe` resolves the child recipe beside the file
  being checked by default (pass `child_recipes_dir=` to override, e.g.
  when checking a staged copy).

**`runners.yaml`** (owner-authored, lives under `$LOCKSTEP_STATE_DIR` —
never the project tree, never agent-writable) is what makes a `runner:`
name resolvable at all:

```yaml
runners:
  claude:
    path: /usr/local/bin/claude   # ABSOLUTE — the engine never PATH-resolves
    models: [haiku, sonnet]       # required, non-empty — fail-closed otherwise.
                                  # The engine always launches with the FIRST
                                  # entry; later entries are reserved for
                                  # future per-marker model selection.
    timeout_minutes: 30           # optional override; falls back to budgets/defaults
budgets:
  max_subcalls_per_run: 8
  max_fractal_depth: 2
```

`path` MUST be an absolute, executable file — the engine checks this again
immediately before every spawn, never from a cached value. A `runner:`
name with no matching entry, or an entry with an empty `models` list, is a
loud start-time refusal (`scenario_start` resolves every marker's runner
before creating the run), never a silent fallback. A marker's `runner:` is
optional — the adapter default (`LOCKSTEP_RUNNER`) applies when absent, and
it passes through to fractal child sessions, so a depth-2 recipe may rely
on it too. See README.md "Subcalls
(v2)" for why the path must be absolute and the state dir must sit outside
the project tree, and for what this setup does and does NOT guarantee.

## `tools:` / local `tools.py` policy — last resort

A recipe's `tools:` block wires python functions as validator nodes. Every fixture and example
in this repo wires exactly one: `run_checks` from `lockstep_mcp.validators` (the generic
check-registry runner — see the dialect crib above). If a recipe's `tools:` entry references a
`module` NOT under `lockstep_mcp.` — i.e. a project-local `tools.py` with bespoke validation
logic — the profile does not reject it, but flags it as a **warning**: "local tools.py: ...
human review recommended". Treat that warning as a hard stop until a human has actually read
the referenced module. A local tool runs arbitrary python as part of the graph; nothing in the
lockstep profile can vet it the way it vets the check registry. Prefer composing new behavior
from the check vocabulary above; reach for a local tool only when nothing here fits, and expect
a human review gate when you do.

## Starting point

Copy `lockstep/recipes/examples/feature-dev.yaml` into `<project>/.lockstep/recipes/` as the
skeleton for any new recipe — it exercises the full hardened vocabulary (`baseline_globs`,
`unchanged`/`changed_in`/`diff_only`/`fresh`, `md_has_sections`, `file_matches`, `junit_gate`)
end to end, in the exact pinned dialect, with the escalate-gate wiring already correct.
Adapt the steps and checks; keep the wiring shape.

For a recipe that needs an independent review gate, copy
`feature-dev-reviewed.yaml` + `review-gate.yaml` together (both, into the
same `.lockstep/recipes/`) instead — the fractal subcall triple, its
`file_matches_hash` verify step, and the child recipe it spawns, wired end
to end.
