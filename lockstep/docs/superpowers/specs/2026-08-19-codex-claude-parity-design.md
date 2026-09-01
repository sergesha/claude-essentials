# Codex and Claude Code Parity Design

Date: 2026-08-19
Status: approved and implemented

## Goal

Make the same lockstep plugin checkout installable and operational in both
Claude Code and Codex without forking the engine, skills, recipes, run state,
or evidence semantics.

Parity includes:

- plugin installation and MCP tool discovery;
- the `scenario_start` → `scenario_status` → work → `scenario_done` loop;
- `SessionStart`, `PreToolUse`, `PostToolUse`, and `Stop` hook behavior;
- policy-gated session binding and stale-session adoption;
- one-shot and fractal subcalls using the host's own CLI by default;
- explicit cross-runner subcalls when a recipe names another configured runner.

The existing deterministic evidence gate remains the load-bearing guarantee.
Host hooks remain an additional policy layer with the limitations already
documented by lockstep.

## Current State

The Python engine, MCP tools, validators, durable run state, origin binding,
and recipe dialect are already host-neutral. The host coupling is concentrated
in four places:

1. `.claude-plugin/plugin.json` is the only package manifest.
2. `hooks/hooks.json` uses Claude-oriented tool matchers and launch commands.
3. `runners.build_argv()` always constructs `claude -p` arguments.
4. Distributed examples and documentation assume `runner: claude`.

The relevant runner, subcall, hook, and session-binding baseline is green:
120 tests pass before this change.

## Decisions

### One plugin root, two host manifests

The repository keeps one plugin root containing the shared implementation:

```text
lockstep/
├── .claude-plugin/plugin.json
├── .codex-plugin/plugin.json
├── .mcp.json
├── scripts/lockstep-plugin
├── hooks/hooks.json
├── skills/
├── recipes/
└── engine/
```

Claude Code reads `.claude-plugin/plugin.json`. Codex reads
`.codex-plugin/plugin.json`, which points at the shared `skills/` directory and
the Codex `.mcp.json`. Both hosts load the same `hooks/hooks.json`.

There will be no copied `adapters/claude` and `adapters/codex` trees. The two
small manifests are the adapters; all behavioral code stays shared.

### A shared launcher and host context preserve project provenance

Add an executable `scripts/lockstep-plugin` launcher. It resolves the plugin
root from its own file location and executes:

```text
uv run --project <plugin-root>/engine lockstep-mcp <verb>
```

The launcher is a POSIX `sh` script because the plugin's currently verified
host matrix is macOS and Linux. It uses its own `$0` location, resolves the
engine directory to an absolute path, and replaces itself with `uv` via
`exec`. Core Python portability is unchanged; native Windows plugin packaging
is not newly claimed by this parity change.

The launcher must not call `chdir`. Claude therefore retains the coding
session's working directory. Codex requires bundled `./` commands to start
with `cwd: "./"`, resolved against the installed plugin root; each Codex tool
call supplies the active workspace in
`_meta.x-codex-turn-metadata.workspaces`. The shared server resolves project
context from that host metadata and falls back to process cwd for Claude and
other clients. The project remains host-supplied and is never an
agent-controlled tool argument.

Both MCP manifests and every hook command use this launcher. This removes
direct `${CLAUDE_PLUGIN_ROOT}/engine` duplication while continuing to ship the
engine from the installed plugin checkout rather than PyPI.

The Codex `.mcp.json` must set `cwd: "./"` so its bundled launcher is
executable. A live acceptance test must prove that an MCP server launched from
the installed plugin root still records the intended workspace in `runs.json`.
A mismatch is a release blocker, not something to paper over by adding a
`project` argument to `scenario_start`.

### Host-specific default runner, recipe-specific override

Each MCP adapter sets one literal environment value:

| Host | MCP environment |
|---|---|
| Claude Code | `LOCKSTEP_RUNNER=claude` |
| Codex | `LOCKSTEP_RUNNER=codex` |

A subcall marker without `runner:` uses this adapter default. A marker with
`runner: claude` or `runner: codex` overrides it and enables an intentional
cross-runner call.

`LOCKSTEP_STATE_DIR` and `LOCKSTEP_RECIPES` are not emitted as unresolved
manifest placeholders. If they are exported by the owner, the MCP process
inherits them. If they are absent, the engine uses its existing defaults:
`~/.lockstep` and `<session-cwd>/.lockstep/recipes`.

### Runner configuration gains an explicit driver

`runners.yaml` entries gain an optional `driver` field:

```yaml
runners:
  claude:
    driver: claude
    path: /absolute/path/to/claude
    models: [claude-haiku-4-5]
  codex:
    driver: codex
    path: /absolute/path/to/codex
    models: [gpt-5.6-luna]
budgets:
  max_subcalls_per_run: 8
  max_fractal_depth: 2
```

Allowed values are exactly `claude` and `codex`. Omitting `driver` means
`claude` for backward compatibility with every existing `runners.yaml`.
Runner names remain arbitrary allowlist keys; behavior is selected by
`driver`, never guessed from the name.

All existing constraints remain unchanged:

- executable paths are absolute and verified adjacent to spawn;
- the model list is required and non-empty;
- the first allowed model is selected when a marker does not select one;
- recipe timeouts can tighten but not exceed owner budgets;
- the state directory cannot sit inside the project;
- child-run nonce credentials remain required for parented run mutation.

## Components and Interfaces

### Claude Code package adapter

Modify `.claude-plugin/plugin.json` to:

- retain its current metadata, skills discovery, and hook reference;
- launch the MCP server through `scripts/lockstep-plugin serve`;
- set `LOCKSTEP_RUNNER` to `claude`;
- stop forwarding unresolved `LOCKSTEP_STATE_DIR` and `LOCKSTEP_RECIPES`
  placeholders.

Existing Claude marketplace installation and tool names remain supported.

### Codex package adapter

Create `.codex-plugin/plugin.json` with:

- stable name `lockstep` and the same semantic version as the Claude manifest
  and Python package;
- the same description, author, homepage, repository, and license metadata;
- `skills: "./skills/"`;
- `mcpServers: "./.mcp.json"`;
- install-surface metadata that does not reference assets that do not exist.

The conventional `hooks/hooks.json` path is used, so the Codex manifest does
not need a custom hook path.

Create `.mcp.json` with this server contract:

```json
{
  "mcpServers": {
    "lockstep": {
      "command": "./scripts/lockstep-plugin",
      "args": ["serve"],
      "env": {"LOCKSTEP_RUNNER": "codex"},
      "cwd": "./",
      "required": true,
      "default_tools_approval_mode": "approve",
      "startup_timeout_sec": 300,
      "tool_timeout_sec": 900
    }
  }
}
```

The relative executable and `cwd` are resolved from the installed plugin
root; the server recovers the active project from Codex tool-call metadata.
Scenario tools are approved by default because a
non-interactive fractal child cannot complete if every lockstep MCP call waits
for a human approval prompt. The 300-second startup budget covers the first
`uv` dependency resolution; the 900-second tool budget exceeds the engine's
600-second default command-check timeout plus process overhead.

The existing repository-level Claude marketplace remains the distribution
catalog. Codex's legacy-compatible marketplace discovery must be verified with
a local installation smoke test. Creating a second marketplace catalog is not
part of this change.

### Shared hook adapter

Keep one `hooks/hooks.json` and one set of Python hook handlers.

The `PreToolUse` matcher becomes:

```text
Write|Edit|NotebookEdit|apply_patch|Bash|Task|Agent
```

This covers Claude's mutation/task names and Codex's canonical
`apply_patch`, `Bash`, and `Agent` paths. The policy intentionally continues
to exclude read-only tools and unrelated write-capable MCP servers; the latter
remains an explicitly documented limitation.

The existing output contracts are retained:

- `PreToolUse` returns canonical `hookSpecificOutput.permissionDecision=deny`;
- `Stop` returns `decision=block` when an active owned run is unreported;
- `SessionStart` returns concise model-visible context;
- `PostToolUse` writes no output and updates only binding sidecars.

The shared handlers continue to consume only fields common to both hosts:
`session_id`, `cwd`, `tool_name`, `tool_input`, and `tool_response`. Codex-only
fields such as `turn_id`, `model`, and `permission_mode` are ignored.
The recorded Codex response uses an object containing a `content` array whose
text block holds the JSON tool result; the existing response walker accepts
this alongside Claude's recorded bare content-block array.

The PostToolUse matcher keeps both known tool-name families:

```text
mcp__lockstep__.*|mcp__plugin_.+_lockstep__.*
```

Unknown MCP prefixes remain safe only when the response carries lockstep's
sibling binding marker, as in the current implementation.

### Runner driver dispatch

Extend `RunnerSpec` with `driver: str`, defaulting to `claude`. Add `driver`
to the accepted runner configuration keys and reject unknown driver values
while loading the owner configuration.

`build_argv(spec, prompt, model, resume_session)` dispatches by driver.

Claude remains byte-for-byte compatible:

```text
<path> -p --output-format json --model <model> -- <prompt>
```

Codex uses:

```text
<path> exec --json --sandbox workspace-write --model <model> -- <prompt>
```

The prompt remains the final token behind `--`, so prompt text cannot be
parsed as an option. Codex `danger-full-access`, approval bypass, hook-trust
bypass, `--ignore-rules`, and `--ignore-user-config` are forbidden defaults.

Runner-session resume remains out of scope for v2. Passing a non-null
`resume_session` to the Codex driver raises `RunnerError` rather than silently
constructing an unverified command shape. The engine already passes `None` in
all production subcall paths.

`child_env()` additionally allows `CODEX_HOME`, enabling an owner-selected
Codex config/auth directory. It does not forward `CODEX_API_KEY`,
`OPENAI_API_KEY`, or arbitrary environment variables. Codex normally reuses
saved CLI authentication under `CODEX_HOME`/`HOME`; missing authentication is
reported as a normal runner failure.

### Runner output parsing

Replace the Claude-only last-line parser with a host-neutral
`extract_session_id(output)` helper:

- a JSON object containing non-empty `session_id` yields the Claude session;
- a Codex JSONL event with `type: "thread.started"` and non-empty
  `thread_id` yields the Codex session;
- malformed, unrelated, and incomplete lines are ignored;
- no recognized id yields `None`.

The session id remains informational in v2. Failing to parse it does not turn
an otherwise successful one-shot subcall into an error. Exit status, timeout,
child-run terminal status, and validated artifacts remain authoritative.

### Host-neutral recipes and skills

Distributed recipes that mean "use this host again" omit `runner:` and rely
on `LOCKSTEP_RUNNER`. Test fixtures that specifically exercise the Claude
driver may retain `runner: claude`; new fixtures explicitly exercise Codex.

`skills/lockstep/SKILL.md` describes the host's subagent capability rather
than requiring a tool literally named `Agent`. Tool discovery continues to
identify lockstep MCP calls by their `scenario_*` suffix.

`skills/lockstep-author/SKILL.md` documents:

- optional adapter-default `runner:` behavior;
- both runner driver configurations;
- explicit cross-runner selection;
- the fact that driver names and allowlist entry names are separate concepts.

## Data Flow

### Main Codex or Claude session

1. The host installs `lockstep` and starts the bundled MCP server through the
   shared launcher.
2. The launcher resolves the installed engine without changing the session
   cwd.
3. `scenario_start` snapshots the recipe and records the session project.
4. PostToolUse binds the returned run id to the host-provided `session_id`.
5. PreToolUse permits mutations only for the bound session when project policy
   requires lockstep.
6. The host follows the same lockstep skill loop and submits artifact pointers.
7. The engine validates evidence and advances independently of host claims.

### Fractal subcall

1. The engine resolves the marker's explicit runner or the adapter default.
2. It resolves the owner-controlled runner spec and driver.
3. It creates the child run and nonce before spawning the CLI process.
4. `child_env()` passes only the approved environment plus the child
   credential and pinned recipes directory.
5. The selected driver builds a Claude or Codex CLI argv.
6. The shared supervisor owns timeout and process-tree termination.
7. The child host loads the same plugin, sees its credential, and drives the
   already-created child run.
8. The parent accepts only the child run's terminal state and hashes from its
   validated baseline snapshot.

No host-specific data is added to recipe state, checkpoints, run records, or
evidence schemas.

## Failure Handling

| Failure | Required behavior |
|---|---|
| Unknown runner driver | Refuse configuration with `RunnerError` before a run is created. |
| Codex or Claude binary missing/non-executable | Existing absolute-path verification refuses spawn. |
| Codex authentication unavailable | CLI exits non-zero; subcall reports runner error without forging progress. |
| Codex emits malformed JSONL | Preserve output and exit status; `session_id` is `None`. |
| Codex hooks are not trusted | Hooks are skipped by the host; `doctor` detects an active run without a binding and explains the trust/matcher remedy. |
| Codex plugin cwd leaks into run provenance | Live provenance smoke fails; release is blocked. |
| Child attempts to mutate another child run | Existing nonce origin binding rejects the MCP mutation. |
| Runner config changes during a live run | Every spawn resolves and verifies the current owner-controlled entry; already spawned processes retain their recorded argv and timeout. |
| Subcall times out | Existing supervisor records timeout, terminates the process tree, and returns an error envelope. |

## Security Properties

This change must not weaken the current security boundary:

- state, bindings, baselines, recipe sources, plugin source, and
  `runners.yaml` still require owner-managed write denial;
- no runner is PATH-resolved by the engine;
- prompts remain behind an option terminator;
- model names remain owner-allowlisted;
- Codex children receive `workspace-write`, never `danger-full-access`;
- no API key is copied into the child allowlist;
- hook trust is never bypassed automatically;
- the engine still validates every `scenario_done` payload itself;
- hooks remain documented as guardrails rather than a complete enforcement
  boundary.

The honest guarantee remains "integrity of the status signal", conditional on
the owner protecting state, recipes, and installed engine files.

## File Changes

### Create

- `.codex-plugin/plugin.json` — Codex package metadata and shared component
  references.
- `.mcp.json` — Codex bundled MCP server configuration.
- `scripts/lockstep-plugin` — plugin-root-resolving launcher for MCP and hook verbs.
- `engine/tests/fixtures/hooks/posttool_scenario_start_codex.json` — payload
  recorded from a real Codex PostToolUse invocation.
- `engine/tests/fixtures/runners/codex-jsonl.txt` — representative Codex JSONL
  stream.
- `engine/tests/test_plugin_packaging.py` — cross-host manifest and launcher
  invariants.

### Modify

- `.claude-plugin/plugin.json` — shared launcher, Claude runner default, and
  removal of unresolved optional env placeholders.
- `hooks/hooks.json` — shared launcher and cross-host mutation matcher.
- `engine/src/lockstep_mcp/runners.py` — driver field, validation, Codex argv,
  and `CODEX_HOME` allowlisting.
- `engine/src/lockstep_mcp/subcalls.py` — dual-format session-id extraction.
- `engine/tests/test_runners.py` — backward compatibility and both argv
  contracts.
- `engine/tests/test_subcalls.py` — Claude JSON and Codex JSONL parsing.
- `engine/tests/test_hooks_cli.py` — Codex canonical tool aliases.
- `engine/tests/test_session_binding.py` — Codex MCP payload binding.
- `engine/tests/test_engine_subcalls.py` — adapter default and Codex driver
  propagation.
- `engine/tests/test_integration_subcalls.py` — fake Codex one-shot and fractal
  paths.
- `recipes/examples/feature-dev-reviewed.recipe.yaml` — adapter-default runner.
- `skills/lockstep/SKILL.md` — host-neutral worker instructions.
- `skills/lockstep-author/SKILL.md` — dual driver reference.
- `README.md` — installation, configuration, trust, security, and smoke tests
  for both hosts.
- `docs/DESIGN.md` and `docs/DESIGN-SUBCALLS.md` — mark the Codex adapter and
  runner as implemented rather than deferred.
- `CHANGELOG.md` — parity feature entry.

No validator, checkpoint, evidence, baseline, or recipe-profile files require
behavioral changes.

## Testing Strategy

### Unit tests

1. Loading a legacy runner entry without `driver` produces
   `driver == "claude"`.
2. `driver: claude` preserves the current argv exactly.
3. `driver: codex` produces the exact `codex exec` argv above.
4. Unknown drivers, disallowed models, relative paths, and Codex resume all
   fail loudly.
5. Hostile prompts beginning with `-` or `--` remain one final argv token.
6. `child_env()` preserves `CODEX_HOME` and excludes API keys and unrelated
   variables.
7. Session extraction handles Claude JSON, Codex JSONL, malformed lines, and
   absent ids.
8. Both recorded PostToolUse payload shapes bind only the touched active run.
9. The shipped PreToolUse matcher covers Claude mutation tools and Codex
   `Bash`, `apply_patch`, and `Agent` aliases.
10. Both manifests point only to files inside the plugin and share one version.

### Integration tests

1. Existing fake Claude one-shot and fractal tests remain unchanged and green.
2. A fake Codex runner accepts the Codex argv, emits JSONL, and completes a
   one-shot subcall.
3. A fake Codex fractal child receives its run id and nonce, completes its child
   run, and produces a hash-pinned artifact accepted by the parent.
4. Policy-gate tests prove a Codex session cannot use another live session's
   run and can adopt it only after the existing stale window.
5. The complete engine test suite passes.

### Packaging and live smoke tests

1. Validate `.codex-plugin/plugin.json` and `.mcp.json` with the current Codex
   plugin validator.
2. Install the checkout through the existing repository marketplace in a clean
   Codex profile; review and trust hooks; confirm both skills and all MCP tools
   appear.
3. In Codex, start a minimal run, inspect status, produce evidence, complete the
   run, and run `doctor` successfully.
4. Enable policy for a scratch project and prove Codex `Bash`, `apply_patch`,
   and `Agent` paths deny outside the bound run and permit inside it.
5. Run a real Codex fractal subcall and verify the parent advances from child
   terminal state and pinned artifact hash.
6. Repeat the minimal run, policy gate, and existing Claude fractal smoke in
   Claude Code to prove no regression.
7. Read each smoke run's `project` from `runs.json` and assert it equals the
   scratch project, never the installed plugin directory.

## Acceptance Criteria

The change is complete only when all of the following are true:

- the same checkout installs in current Claude Code and current Codex;
- both hosts discover the same two skills and the same MCP tool surface;
- both hosts complete the same minimal recipe with identical engine state and
  evidence semantics;
- policy gating is session-bound in both hosts;
- an omitted subcall `runner` selects the current host's CLI;
- explicit `runner: claude` and `runner: codex` select the named owner entries;
- one-shot and fractal Codex subcalls pass automated and live smoke tests;
- legacy Claude `runners.yaml` files work without edits;
- no test requires weakening state-dir protection, hook trust, sandboxing,
  model allowlists, or origin binding;
- the full test suite is green;
- documentation states remaining hook and same-user OS limitations without
  claiming stronger enforcement than the engine provides.

## Non-goals

- publishing the engine to PyPI or replacing plugin-local distribution;
- adding Gemini, Copilot, or another runner driver;
- resuming Codex or Claude runner sessions in v2;
- automatically installing, trusting, or bypassing host hooks;
- forwarding API keys into subcall environments;
- changing the yamlgraph recipe dialect or evidence vocabulary;
- making write-capable tools from unrelated MCP servers part of the policy
  matcher;
- public universal-directory submission or creation of a second marketplace;
- native Windows plugin-launcher packaging or a Windows live-smoke matrix;
- redesigning the existing same-user OS threat model.

## Documentation Sources

- OpenAI plugin packaging and bundled MCP/hooks:
  <https://developers.openai.com/plugins/build/plugins>
- Codex hook events, fields, matchers, trust, and tool coverage:
  <https://learn.chatgpt.com/docs/hooks>
- Codex non-interactive JSONL, sandboxing, authentication, and resume behavior:
  <https://learn.chatgpt.com/docs/non-interactive-mode>
- Codex skill discovery and `SKILL.md` structure:
  <https://learn.chatgpt.com/docs/build-skills>
