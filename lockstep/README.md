# lockstep

Lockstep is a durable workflow engine for coding agents. It compiles declarative
workflow sources to yamlgraph/LangGraph recipes, parks at explicit work steps,
and advances only after deterministic validation of the submitted evidence.

## Product model

The engine owns workflow state, recovery, validation, artifacts, and external
publication. The coding agent performs work in the project and submits evidence;
its prose is never treated as proof. Pinned commands, source snapshots, project
paths, artifact digests, effect receipts, and terminal states are checked by the
runtime.

The two packaged templates are real end-to-end workflows:

- `reviewed-change` performs a change, verifies it, requests independent review,
  and reaches a durable terminal result.
- `parallel-review` runs independent review branches and joins their results.

Both are available through `template list`. They exercise the same runtime used
by authored workflows; they are not illustrative-only files.

## Install and launch

The repository ships Claude and Codex plugin manifests. Both start
`scripts/lockstep-plugin`, which installs the pinned environment and then runs
the same Python package. From a local checkout root, run
`scripts/lockstep-install`, then verify that installed environment with
`uv run --project engine --no-sync lockstep doctor`. Install Python 3.11 or
newer and `uv` first; installation needs access to the locked dependencies.
The installer creates `engine/.venv`; it does not add `lockstep` to your PATH.
For the shell examples below, run this from the **lockstep plugin directory**
(the directory containing this README), then change to your target project:

```bash
export LOCKSTEP_PLUGIN_ROOT="$(pwd -P)"
lockstep() { "$LOCKSTEP_PLUGIN_ROOT/scripts/lockstep-plugin" "$@"; }
```

The absolute launcher keeps the target project as the working directory. An
installed plugin uses the same launcher automatically for MCP and hooks.

Claude Code, through the existing marketplace:

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install lockstep@claude-essentials
```

Codex, from a local checkout of the repository root:

```bash
codex plugin marketplace add /absolute/path/to/claude-essentials --json
codex plugin add lockstep@claude-essentials --json
```

For a custom state location, export an absolute path in the same shell used for
Lockstep provisioning and for starting the plugin host from the target project:

```bash
export LOCKSTEP_STATE_DIR=/absolute/path/to/owner-state
# Start Claude Code or Codex here.
```

Restart the active plugin host after changing this value. The host forwards
`LOCKSTEP_STATE_DIR` to the bundled MCP server; the plugin does not store or
choose its value.
When unset, state defaults to `~/.lockstep`. Keep recipes in the active
project's `.lockstep/recipes` for the CLI/provisioning/plugin workflow.

For Codex, start it interactively once and approve the installed hooks when
prompted. Do not use hook-trust or approval bypass flags for normal operation.

Codex receives a non-authoritative `LOCKSTEP_PLUGIN_HOST=codex` launcher marker;
Claude Code supplies its native `CLAUDECODE` marker. Template initialization may
use those markers as a convenience default. The launcher uses the Codex marker
only to recover `CODEX_HOME` from the installed plugin path, and the runtime
never treats either marker as execution authority.

## Authority and threat model

Lockstep is a **Local unsandboxed single-user** product. Its process has ambient
OS-user authority. The owner-controlled state, plugin, workflow source,
credentials, and executable paths, together with Python, uv,
yamlgraph/LangGraph, the host, and the operating system, are the TCB (trusted
computing base). Ambient OS-user authority describes process power and TCB
exposure, not an authorization source.

Lockstep is **not security confinement** and provides **no constrained-runner,
broker, or sandbox guarantee**. A hostile process with the same OS-user rights
can read or alter anything that user can access. Host permissions and operating
system isolation remain the owner's responsibility. No configuration or report
text grants authority. Configuration, manifests, templates, recipes, reports,
artifact digests, run IDs, PASS strings, and host markers are non-authoritative.
Managed and pinned OS-user execution requires an exact owner-selected runtime
grant, resolved and revalidated at commitment. Publication separately requires
a fresh exact bearer bound to the named commitment.

## Authoring workflows

Canonical workflow sources live at
`.lockstep/workflows/NAME.workflow.yaml`; compiled recipes live at
`.lockstep/recipes/NAME.recipe.yaml`.

The complete CLI authoring grammar is:

```text
recipe init NAME
recipe compile NAME
recipe check [NAME | --all]
recipe diff NAME
recipe render NAME --view workflow|generated
recipe estimate NAME [--json]
template list
template show TEMPLATE NAME
template init TEMPLATE NAME [--runner claude|codex]
```

Prefix these forms with `lockstep` in a shell. Names are logical names, not YAML
path arguments. `recipe estimate` returns the closed
`lockstep.structural-estimate/v1` schema, including
`peak_parallel_child_calls`.

The matching MCP authoring surface is:

```text
recipe_init
recipe_compile
recipe_check
recipe_diff
recipe_render
recipe_estimate
template_list
template_show
```

A normal template flow is:

```bash
lockstep template list
lockstep template show reviewed-change demo
lockstep template init reviewed-change demo --runner claude
lockstep recipe compile demo
lockstep recipe check demo
lockstep recipe render demo --view workflow
lockstep recipe render demo --view generated
lockstep recipe estimate demo --json
```

Use `--runner claude` for native Claude managed reviews or `--runner codex` for
native Codex managed reviews. Without the flag, a recognized plugin host is the
default; a standalone shell with no host marker retains the Codex default for
compatibility. The selected runner is written into every managed child call
before compilation. It is still only a workflow requirement: owner provisioning
must separately grant the exact selected installation. `parallel-review` is
initialized the same way and brings its declared child workflow sources with
it. Compilation is child-first and produces the parent recipe plus dependency
and source-map artifacts.

## Manual yamlgraph

Manual yamlgraph remains a first-class, marker-free path. Put a valid yamlgraph
file at `.lockstep/recipes/NAME.recipe.yaml` without a corresponding workflow
source. Then use `recipe check NAME`, `recipe render NAME --view generated`, and
`recipe estimate NAME --json`. The runtime detects the canonical manual file by
layout; no marker, compatibility stanza, or hidden selector is required.

Manual yamlgraph is subject to the same closed ingress, project-path, runtime,
evidence, recovery, artifact, and publication contracts as compiled workflows.
It does not gain authority from fields in the YAML.

Manual recipes may use the exact installed
`lockstep.runtime.validators.run_checks` verdict relay without an executable
grant. The engine checks submitted evidence before supplying its verdict;
the relay itself does not execute recipe commands. Other Python callables and
shell tools retain their executable-authority requirements. Manual completion
supports project-read checks; process checks require the pinned execution path.
Malformed evidence or inadmissible checks leave the current step pending;
ordinary failed read checks follow the manual recipe's authored retry edges.

## Running a workflow

Before running either packaged template, complete [owner runtime setup](#owner-runtime-setup).
Both templates use the managed runner selected at initialization. A Claude-only
installation can initialize with `--runner claude`; a Codex-only installation
can use `--runner codex`. `reviewed-change` assumes a Python project with `src/`,
`tests/`, and `pytest` available on the configured direct-local command PATH.
Its verification checks the pinned command's exit status; it does not impose a
separate skipped-test limit or prove that the tests cover the requested change.

Start a recipe with `scenario_start`, then repeat:

1. Call `scenario_status` and read the current task, exit criterion, and required
   evidence.
   Worker status includes the admitted task, exit criterion, and declared
   evidence/check fields. Inspect `artifact_contract` and `writes` when present.
2. Perform only the current step's work.
3. Call `scenario_done` with the exact run id, step name, and evidence payload.
4. If validation fails, use the returned diagnostics and retry that step. If it
   passes, call `scenario_status` again.

Use `scenario_abort` or `scenario_escalate` only for their documented lifecycle
transitions. A terminal status is complete only when returned by the engine.
`reviewed-change` and `parallel-review` use native child workflow calls and
runtime effects; their acceptance, lineage, and receipts are durable and
machine-checked.

When status includes `parallel_progress.steps`, each entry describes eligible
pending manual work and carries its exact step selector. The overall `owner`
and `next_action` remain authoritative: while the engine owns the run, follow
its wait instruction before editing or submitting evidence. For an ambiguous
abort or escalation, supply the intended `step` to the MCP tool, or `--step`
to the corresponding CLI command.

For a standalone CLI run, select a worker session when starting and reuse it
for mutations. This binds only the newly created run; it does not adopt an
existing session. Run these commands from the same project and use the same
owner state throughout:

```bash
lockstep scenario start demo --session-id terminal-worker
# Copy run_id and the exact current step from the returned status.
lockstep scenario status RUN_ID
lockstep scenario done RUN_ID STEP --session-id terminal-worker --evidence '{}'
```

Replace `{}` with the current step's required evidence. Use
`lockstep scenario abort RUN_ID --session-id terminal-worker --step STEP` or
`lockstep scenario escalate RUN_ID 'reason' --session-id terminal-worker --step STEP`
when that lifecycle transition is intended. In a plugin host, MCP plus its
PostToolUse hook establishes the host session binding instead.

## Owner runtime setup

Run these commands yourself from the target project after compiling `demo`.
Provisioning selects executable authority; an agent's report or generated
configuration does not authorize it. Manual workflows without managed or
pinned effects do not need runtime grants.

Configure only runners listed by the selected recipes. Ordinary checks should
normally use the `direct-local` pinned backend. It runs literal argv without a
shell, with the configured PATH, cwd, timeout, bounded capture, and cancellation;
it needs no model, AI CLI, or AI home. It is local OS-user execution, not an OS
sandbox or confinement boundary.

Keep `LOCKSTEP_STATE_DIR` outside the project. Create an owner-only input/TMPDIR
and save the configuration as `$LOCKSTEP_OWNER_INPUTS/config.json`:

```bash
export LOCKSTEP_STATE_DIR="$(python3 -c 'import os; from pathlib import Path; print(Path(os.environ.get("LOCKSTEP_STATE_DIR") or "~/.lockstep").expanduser().resolve())')"
mkdir -p -m 700 "$LOCKSTEP_STATE_DIR"
export LOCKSTEP_OWNER_INPUTS="$(mktemp -d "$LOCKSTEP_STATE_DIR/provision.XXXXXX")"
```

This is the complete direct-only configuration; use the real command search
PATH needed by the recipe:

```json
{
  "schema": "lockstep.runtime-provision-config/v1",
  "pinned": {
    "backend": "direct-local",
    "environment": {
      "PATH": "/usr/bin:/bin",
      "LANG": "C",
      "LC_ALL": "C",
      "TMPDIR": "/absolute/owner-only/tmp"
    }
  }
}
```

For a managed template, add exactly one member matching its `--runner` value.
Native Claude uses normal CLI login from the native user home; Lockstep does not
copy or log credentials:

```json
"claude": {
  "executable": "/absolute/path/to/claude",
  "model": "owner-selected-model",
  "home": "/absolute/native/user/home",
  "environment": {"PATH": "/actual/path", "LANG": "C", "LC_ALL": "C", "TMPDIR": "/absolute/owner-only/tmp"}
}
```

Native Codex uses its existing authenticated home. `cli_version` records the
exact output of the selected executable's `--version`; it is not a required
version range or release pin.

```json
"codex": {
  "executable": "/absolute/path/to/codex",
  "model": "owner-selected-model",
  "cli_version": "exact installed CLI version output",
  "permission_profile": {"sandbox": "workspace-write", "approval": "never"},
  "codex_home": "/absolute/authenticated/codex-home",
  "environment": {"PATH": "/actual/path", "LANG": "C", "LC_ALL": "C", "TMPDIR": "/absolute/owner-only/tmp"}
}
```

The legacy Codex-backed pinned backend remains explicitly supported. Use the
same exact Codex binding fields under `pinned`, add
`"pinned_permission_profile": "existing-owner-selected-profile"`, and use a
separate credential-free Codex home. It invokes `codex sandbox` without an LLM
call, never silently replaces direct-local, and does not change the local
unsandboxed threat model.

List requirements after creating the complete JSON document:

```bash
lockstep owner list-runtime-requirements --project "$PWD" --recipe demo \
  > "$LOCKSTEP_OWNER_INPUTS/requirements.json"
python3 -m json.tool "$LOCKSTEP_OWNER_INPUTS/requirements.json"
```

Review each requirement's `uses`, runner, and authorities. The next block
selects **every requirement just listed** for `demo`; run it only when that is
your intended grant set. For a subset, write only the approved
`grant_selection_key` strings as a JSON array in `grants.json` instead.
`--replace-grants` replaces the complete grant set for this owner state; it
does not append grants, and can revoke grants needed by other runs. For
multiple recipes in this project, repeat `--recipe NAME` in both commands and
review the combined inventory. Re-list and provision after changing a workflow
or selected installation; grants are bound to exact requirements and bindings.

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ['LOCKSTEP_OWNER_INPUTS'])
requirements = json.loads((root / 'requirements.json').read_text())
keys = [item['grant_selection_key'] for item in requirements['requirements']]
with (root / 'grants.json').open('x') as output:
    json.dump(keys, output, indent=2)
PY
lockstep owner provision-runtime --project "$PWD" --recipe demo \
  --config "$LOCKSTEP_OWNER_INPUTS/config.json" \
  --replace-grants "$LOCKSTEP_OWNER_INPUTS/grants.json"
```

Provisioning validates and captures supplied bindings; it does not run a review
or prove that the selected model, native login, or project dependencies will
succeed together. Publication consent remains a separate interactive action.

## Artifact publication and consent

Publication stops at an accept step. From the project directory, the owner can
preview and issue consent with:

```bash
lockstep consent issue --run RUN_ID --step STEP_ID
```

The command displays the exact commitment and requires an interactive digest
confirmation. The bearer token is shown once and is stored only by its hash.
Redeem it with `lockstep consent accept`, or through the token-bearing MCP
acceptance tool. Acceptance is bound to the artifact, producer coordinate,
destination, transformation, audience, project, run, and step; retries return
the same durable receipt and cannot retarget the consent.

## Configuration and recovery

`LOCKSTEP_STATE_DIR` defaults to `~/.lockstep` and holds durable checkpoints,
run records, snapshots, artifact metadata, effect journals, consent state, and
policy bindings. MCP recipe lookup supports a `LOCKSTEP_RECIPES` override,
defaulting to the active project's `.lockstep/recipes` directory. This override
does not relocate CLI authoring, runtime provisioning, scenario, or consent
operations: those still use project-local recipes. The bundled Codex plugin
therefore uses the project-local recipe directory, not this override.
Keep owner state outside the project and protect
it, the installed plugin, and workflow sources according to the threat model.

The runtime snapshots admitted workflow inputs and uses durable checkpoints and
journals. After interruption, status and command operations recover the same
run rather than inventing a new one. Delivered effects and accepted publications
are replay-safe: the runtime verifies their durable identity and lineage before
returning an existing result.

To investigate an escalated run, use `lockstep scenario events RUN_ID` or the
`scenario_events` MCP tool. Failed effect events include `fixed_error_code`
when a categorized failure was recorded; for example, `manifest_invalid`
indicates a manifest validation failure. Events do not expose effect results
or raw exception text. Not every escalation has an effect error code: an
intentional failure transition or a native task error may have none.

## Development verification

From `engine/`:

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check --select E9,F63,F7,F82 src tests
```

The Ruff command intentionally checks the stable runtime-impact rule subset;
it is not a claim that the repository has adopted Ruff's full style policy.

The installed-contract suite builds a clean wheel and a staged plugin, exercises
authoring commands from foreign working directories, and verifies installed
imports and packaged resources. Runtime and integration suites exercise
workflow, recovery, effect, and publication contracts with controlled fixtures
and providers. Those checks are not a claim that authenticated Codex reviews
ran end to end against a real account; that requires a separately authorized
live run with the owner's selected configuration.
