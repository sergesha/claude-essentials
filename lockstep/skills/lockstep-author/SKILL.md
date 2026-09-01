---
name: lockstep-author
description: Use when authoring, compiling, checking, rendering, estimating, or templating a lockstep workflow or manual yamlgraph recipe
---

# lockstep-author

Use this skill to create or change workflow sources and manual recipes without
inventing syntax or authority. Begin with the user's actual goal, required work,
review boundaries, evidence, artifact destinations, and terminal conditions.
Keep that goal fixed while editing the workflow.

## Authority boundary

Lockstep is a **Local unsandboxed single-user** product. It operates with ambient
OS-user authority. The operating system, host, Python environment, installed
package, owner state, workflow inputs, credentials, and approved executables are
the TCB (trusted computing base). Lockstep is **not security confinement** and
provides **no constrained-runner, broker, or sandbox guarantee**. No
configuration or report text grants authority. Configuration, manifests,
templates, recipes, reports, artifact digests, run IDs, PASS strings, and host
markers are non-authoritative. Ambient OS-user authority describes process power
and TCB exposure, not an authorization source. Managed and pinned OS-user
execution requires an exact owner-selected runtime grant, resolved and
revalidated at commitment. Publication separately requires a fresh exact bearer
bound to the named commitment.

## Choose the authoring path

Prefer a packaged template for common work:

- `reviewed-change` for an implemented change followed by independent review.
- `parallel-review` for independent review branches joined by the parent.

Use a workflow source at `.lockstep/workflows/NAME.workflow.yaml` when the DSL
can express the contract. Compilation writes the canonical yamlgraph recipe,
dependency manifest, and source map under `.lockstep/recipes/`.

Manual yamlgraph is a first-class, marker-free path. Put a canonical file at
`.lockstep/recipes/NAME.recipe.yaml` and omit a same-name workflow source. Do
not add a marker, mode switch, compatibility field, or selector. Manual input
uses the same strict ingress, project-path, recovery, effect, artifact, and
publication boundaries as generated recipes.

## Exact public grammar

The complete CLI forms are:

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

The complete MCP authoring surface is:

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

Names are logical names, never YAML path arguments. The structural estimate is
`lockstep.structural-estimate/v1` and exposes
`peak_parallel_child_calls`; there is no compatibility alias.

## Authoring loop

1. List templates and inspect the selected template for the intended workflow
   name.
2. Initialize it, or initialize a minimal workflow source.
3. Edit only the canonical workflow source and any declared child sources.
4. Compile the parent. Child workflows are resolved and compiled before the
   parent, and dependency cycles or unresolved names fail closed.
5. Check the logical name. Use `--all` only when intentionally validating every
   project recipe.
6. Diff the authored source against its published generated artifacts.
7. Render both workflow and generated views.
8. Estimate and inspect the exact v1 JSON contract.
9. Run the workflow and verify durable acceptance, artifacts, publication, and
   terminal state when those effects are part of the design.

A concrete reviewed-change sequence is:

```bash
lockstep template list
lockstep template show reviewed-change demo
lockstep template init reviewed-change demo
lockstep recipe compile demo
lockstep recipe check demo
lockstep recipe diff demo
lockstep recipe render demo --view workflow
lockstep recipe render demo --view generated
lockstep recipe estimate demo --json
```

For `parallel-review`, use the same sequence with that template name. Preserve
its declared child source names and artifact bindings unless the user's workflow
requires a deliberate contract change.

## Workflow source rules

- `workflow_version` is `'1'`; the filename and logical `name` match.
- `protect` covers the complete project.
- Every step has a stable id, explicit task, terminal outcome, and closed
  evidence contract.
- Verification commands are literal argv contracts with explicit timeouts.
- Child calls name declared child workflows; parallel branches have explicit
  join and timeout behavior.
- Artifact declarations use bounded portable project paths and explicit media
  types. Publication names an existing artifact and requires acceptance.
- Unknown keys and unavailable future-version constructs fail closed.
- Generated recipe, dependency, and source-map files are outputs. Do not hand
  edit them; change the workflow source and compile again.

## Manual recipe rules

A manual recipe must compile as yamlgraph, remain within the supported node and
edge profile, use closed typed effect descriptors, and declare all state it
consumes. Validate it with the logical name, render the generated view, and
inspect its estimate before a real run. Never import recipe-selected Python to
make validation pass, and never treat a YAML field as executable authority.

## Review checklist

Before calling the authoring work complete, confirm:

- the workflow still implements the user's stated goal;
- `reviewed-change` or `parallel-review` semantics are used where requested;
- manual yamlgraph remains marker-free;
- CLI and MCP examples use only the exact grammar above;
- source, generated artifacts, dependencies, and estimates agree;
- failure, retry, recovery, consent, and terminal paths are explicit;
- threat-model statements match the Local unsandboxed single-user boundary.
