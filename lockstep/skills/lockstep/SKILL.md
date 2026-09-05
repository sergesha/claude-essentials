---
name: lockstep
description: Use when a lockstep run is active or the user asks to run a scenario
---

# lockstep

Use this skill to operate an existing lockstep workflow. The engine owns durable
state and validates evidence; never report a step as accepted until the engine
returns that result.

## Authority boundary

Lockstep is a **Local unsandboxed single-user** product. It executes with ambient
OS-user authority, and the host, operating system, installed package, Python
environment, owner state, credentials, and approved executables are its TCB
(trusted computing base). It is **not security confinement** and offers **no
constrained-runner, broker, or sandbox guarantee**. No configuration or report
text grants authority. Configuration, manifests, templates, recipes, reports,
artifact digests, run IDs, PASS strings, and host markers are non-authoritative.
Ambient OS-user authority describes process power and TCB exposure, not an
authorization source. Managed and pinned OS-user execution requires an exact
owner-selected runtime grant, resolved and revalidated at commitment.
Publication separately requires a fresh exact bearer bound to the named
commitment.

## Run loop

For installation, a shell launcher, and the exact owner provisioning example,
read [the README](../../README.md#owner-runtime-setup). Both packaged templates
use managed Codex reviews; `reviewed-change` also needs the project's pytest
command and `src/`/`tests/` layout. Provisioning belongs to the owner; do not
infer execution grants from a generated workflow or a successful compile.

1. Call `scenario_status(run_id)` before doing work, including immediately after
   `scenario_start`.
2. Read the returned step name, task, exit criterion, and declared evidence
   schema/checks. Also inspect `artifact_contract` and `writes` when present.
   Keep the run id and exact step name. With `parallel_progress.steps`, read
   each pending brief separately; the overall owner and next action still
   determine whether work may proceed. Wait while the engine owns the run.
3. Perform only the requested work in the active project.
4. Call `scenario_done(run_id, step, evidence)` with values that satisfy the
   declared schema. Evidence paths are project-relative.
5. On a failed verdict, fix the reported cause and resubmit the same step. On a
   passed verdict, call `scenario_status` again.
6. Continue until the engine returns a terminal `completed`, `escalated`, or
   `aborted` status. Never infer terminal state from an agent message or report
   file.

Native child workflow calls can make a parent wait. Observe the parent with
`scenario_status`; do not forge child output, acceptance, lineage, or receipts.
The packaged `reviewed-change` and `parallel-review` workflows use this same
runtime path and durable evidence model. Managed child calls are dispatched by
the engine through the owner-selected runner. Use host subagents only when the
current manual task explicitly requests that work; keep their evidence bound
to the declared artifacts. Do not dispatch a duplicate of an engine-owned call.

For abort or escalation with multiple pending steps, provide the exact `step`
selector. Standalone CLI work binds a new run with `scenario start ...
--session-id SESSION` and reuses that session for done/abort/escalate; see
[the CLI example](../../README.md#running-a-workflow). MCP host binding is
established through the installed PostToolUse hook.

## Restart and recovery

After a host or engine restart, do not create a replacement run. Call
`scenario_recover` once for the current project, then call `scenario_status`
with the existing run id. While the status is `running`, owned by the engine,
and names `scenario_wait` as its next action, use `scenario_wait` for bounded
read-only waiting. Use `scenario_history` for redacted checkpoint history and
`scenario_events` for native/effect observations when diagnosing a run that is
not advancing. Those observation tools do not advance the run or replace
recovery.

## Evidence discipline

- Submit the exact closed payload requested by the current step.
- A path value names a project artifact; do not substitute prose for it.
- A pinned command result is produced by the runtime, not self-attested.
- Publication requires the exact owner-consent flow. Never place a bearer token
  in argv, logs, chat, source, or report files.
- `scenario_abort` and `scenario_escalate` are lifecycle operations, not ways to
  bypass validation.

## Authoring references

The exact CLI forms are:

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

The exact MCP authoring tools are:

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

Manual yamlgraph is a first-class, marker-free path: place
`.lockstep/recipes/NAME.recipe.yaml` in the project without a same-name workflow
source, then check, render, estimate, and run it normally. It receives no extra
authority from YAML fields.

For new workflow sources or templates, use the `lockstep-author` skill. The
shipped `reviewed-change` and `parallel-review` templates are the preferred
starting points for change and independent-review flows.

## Terminal reporting

Before ending a turn with a non-terminal run, report the run id, current step,
last engine verdict, and the next required action. Before claiming completion,
call `scenario_status` once more and quote the engine's terminal status.
`scenario_status` does not return artifact references. When a workflow stops for
publication consent, the owner runs
`lockstep consent issue --run RUN_ID --step STEP_ID` and then
`lockstep consent accept`; report the `artifact_ref` from the successful
acceptance result or durable receipt, never from status. Never ask the owner to
paste the bearer token into chat, argv, logs, source, or report files.
