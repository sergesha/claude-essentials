# Direct operations

## Native owners

Backlog.md is the sole owner of product intent and priority; use its native interface or files for product work.

OpenSpec is the sole owner of proposals, requirements, design, and specification tasks; use its native interface for specification work.

Beads/Dolt is the sole owner of executable decomposition, dependencies, readiness, claim, blockers, and completion; use its native interface for execution-graph work.

For already-selected implementation work, invoke only the applicable Superpowers skills as a bounded leaf, then return control to SpeciFlow.

Product Git and CI own source changes, review history, dirty state, and
implementation evidence. They do not own OpenSpec approval or Beads status.

## Native roots and isolated repositories

Keep native-root selection at skill level, outside the storage helper. Accept
only the unique, canonical absolute roots `planning`, `backlog`, `openspec`,
and `beads`. An invocation-explicit root wins. Otherwise compare the already
initialized deterministic root below the selected data root with the
documented native root query or marker: one valid candidate is selected, two
different candidates are `ambiguous`, and a similar directory name is not
discovery. The deterministic roots are `<data-root>/planning`,
`<data-root>/planning/backlog`, `<data-root>/planning/openspec`, and
`<data-root>/beads`; never persist them in a locator.

Put only planning documents and configuration in the isolated `planning/` Git
repository. Keep the Beads/Dolt native root in sibling `beads/`, verify it with
`bd where`, and ensure planning Git cannot capture it. A Dolt commit and a
planning Git commit are distinct owner effects. Product `.gitignore`, hooks,
attributes, and Git configuration changes require separate approved product
actions and are never init effects.

## Approved mutations

Inspect live native state, propose exactly one action and its exact effects, wait for explicit approval, invoke the sole owner's native interface or approved integration, inspect the native result, then commit only native data in the approved data repository.

For each native action inspect its interface, selected root, revision and dirty
state, available preview, and post-check. Every planning preview says
`commit: automatic` or `commit: none`. A clean isolated planning mutation
defaults to an automatic exact-path commit unless the user approves
`commit: none`; never auto-commit a product, skill-source, package-source, or
`claude-essentials` repository. If `planning/` is not already Git, preview a
separate `git init` native action with separate approval or `commit: none`;
never hide Git initialization in storage init or document mutation.

For Beads, observe the native Dolt commit policy and use only documented native
Beads commit behavior. Required but unperformed automatic commits are
`incomplete`; lost or ambiguous output is `unknown`. Use a documented atomic claim
when available; otherwise prohibit automatic claim and require manual native
assignment. A guessed ID or local lock is not claim or create evidence.
Never hide an owner result with planning Git or duplicate the same owner's
native commit.

On failure, lost or ambiguous output, or an uncertain create result, stop and report native evidence and uncommitted changes.

Never invent recovery state, blindly repeat a create, or promise uniqueness from a text or token search.

## OpenSpec to Beads

The optional upstream `openspec-to-beads` material is pinned to
`lucastamoios/celeiro` commit
`4c3cf508b3fd8a040d6cf99d4c887056cafe482d` and its complete recursive
`.claude/skills/openspec-to-beads/` subtree. Fetch only that closure into a
private temporary non-discovery path. It is untrusted heuristic guidance, not
an owner, and is never registered or installed as a skill.

The upstream `lucastamoios/openspec-to-beads` skill advertises automatic and proactive triggers, so it must not remain in an always-discoverable skill set.

Only after SpeciFlow has proposed the exact OpenSpec-to-Beads analysis and the user has explicitly approved that analysis may the agent load the integration. Load it only for the approved invocation, with automatic and proactive activation disabled. Never install or enable it persistently, globally, or for the whole project.

First use the invocation-scoped integration for read-only analysis only. Present the exact proposed Beads issues and dependencies without creating them. Wait for explicit approval of that conversion preview before allowing any write. Then separately approve normal primary-agent native Beads writes; never delegate mutation or proactive control to the integration.

If the host cannot disable implicit activation, do not load or install the integration. Stop and offer an invocation-scoped explicit-only method instead.

Acquire the integration only from its [pinned canonical subtree](https://github.com/lucastamoios/celeiro/tree/4c3cf508b3fd8a040d6cf99d4c887056cafe482d/.claude/skills/openspec-to-beads).

If the upstream material, fetch, or analysis is unavailable, report it and keep direct manual projection available. Analyze the approved OpenSpec change directly, present an item-by-item native Beads write preview, and require explicit user confirmation before those writes. The agent may also offer an invocation-scoped, explicit-only loading method after approval, but never persistent or global installation.

Never copy or reimplement its conversion algorithm, templates, priority or dependency rules, gap heuristics, or issue schema.

Before creation, deduplicate automatically only when a stable OpenSpec
reference matches exactly one native Bead. Zero permits an approved create,
multiple matches are `ambiguous`, and no stable reference requires item-by-item
confirmation. On partial creation, report created native IDs and preview only
remaining items. Unknown output is never retried.

## Live queries

Read `status` live from the four native tools and persist nothing.

Derive `next` live as one non-authoritative action, present it for approval, and persist no recommendation, phase, cursor, or queue.
