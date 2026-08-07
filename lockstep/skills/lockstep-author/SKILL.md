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

The dialect below is **frozen by spike** against yamlgraph 0.5.18 — it is copied verbatim from
`lockstep/engine/tests/fixtures/recipes/good/two-steps.yaml` and
`lockstep/recipes/examples/feature-dev.yaml`, the two real fixtures the engine's own tests
compile and run. Do not improvise a different shape for any of the traps below — each one was
a real failure mode during the engine's own spike.

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
end to end, in the exact spike-frozen dialect, with the escalate-gate wiring already correct.
Adapt the steps and checks; keep the wiring shape.
