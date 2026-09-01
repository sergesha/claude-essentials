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
`uv run --project engine --no-sync lockstep doctor`.

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

Start Codex interactively once and approve the installed hooks when prompted.
Do not use hook-trust or approval bypass flags for normal operation.

Codex receives a non-authoritative `LOCKSTEP_PLUGIN_HOST=codex` launcher marker.
The launcher uses it only to recover `CODEX_HOME` from the installed plugin path.
The runtime never reads that marker.

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
template init TEMPLATE NAME
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
lockstep template init reviewed-change demo
lockstep recipe compile demo
lockstep recipe check demo
lockstep recipe render demo --view workflow
lockstep recipe render demo --view generated
lockstep recipe estimate demo --json
```

`parallel-review` is initialized the same way and brings its declared child
workflow sources with it. Compilation is child-first and produces the parent
recipe plus dependency and source-map artifacts.

## Manual yamlgraph

Manual yamlgraph remains a first-class, marker-free path. Put a valid yamlgraph
file at `.lockstep/recipes/NAME.recipe.yaml` without a corresponding workflow
source. Then use `recipe check NAME`, `recipe render NAME --view generated`, and
`recipe estimate NAME --json`. The runtime detects the canonical manual file by
layout; no marker, compatibility stanza, or hidden selector is required.

Manual yamlgraph is subject to the same closed ingress, project-path, runtime,
evidence, recovery, artifact, and publication contracts as compiled workflows.
It does not gain authority from fields in the YAML.

## Running a workflow

Start a recipe with `scenario_start`, then repeat:

1. Call `scenario_status` and read the current task, exit criterion, and required
   evidence.
2. Perform only the current step's work.
3. Call `scenario_done` with the exact run id, step name, and evidence payload.
4. If validation fails, use the returned diagnostics and retry that step. If it
   passes, call `scenario_status` again.

Use `scenario_abort` or `scenario_escalate` only for their documented lifecycle
transitions. A terminal status is complete only when returned by the engine.
`reviewed-change` and `parallel-review` use native child workflow calls and
runtime effects; their acceptance, lineage, and receipts are durable and
machine-checked.

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
policy bindings. `LOCKSTEP_RECIPES` defaults to the active project's
`.lockstep/recipes` directory. Keep owner state outside the project and protect
it, the installed plugin, and workflow sources according to the threat model.

The runtime snapshots admitted workflow inputs and uses durable checkpoints and
journals. After interruption, status and command operations recover the same
run rather than inventing a new one. Delivered effects and accepted publications
are replay-safe: the runtime verifies their durable identity and lineage before
returning an existing result.

## Development verification

From `engine/`:

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check --select E9,F63,F7,F82 src tests
```

The Ruff command intentionally checks the stable runtime-impact rule subset;
it is not a claim that the repository has adopted Ruff's full style policy.

The installed-contract suite additionally builds a clean wheel and a staged
plugin, runs complete template and manual flows from foreign working
directories, verifies import/resource isolation, and checks active guidance and
manifests against this contract.
