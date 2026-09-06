# Initialization preview

The user-facing preview has two parts: the complete setup outline and the
current action below. Brevity shortens cells while retaining all applicable
items and the four outline fields.

For an explicit setup or full-stack initialization request, inspect all
selected owners and show the complete ordered checklist before asking for the
first approval. Include only applicable steps: owner dependency installation,
storage initialization (not only read-only resolution), planning Git init when
required, each applicable native-owner initialization with its commit effect,
and a final live status check.

## Complete setup outline

The actual reply contains a compact ordered table: operation and target,
expected effects, commit policy/effects, and missing inspection. Use separate
rows for storage initialization, planning Git, and each selected native owner.
Show known policy even when native effects remain uninspected. For example,
with dependencies installed and roots absent, the outline is:

| Operation and target | Expected effects | Commit policy/effects | Missing inspection |
| --- | --- | --- | --- |
| Storage init at resolved data root | Create collision metadata only | No planning Git or Dolt commit | Literal metadata path and complete JSON |
| Planning Git init at `planning/` | Create isolated repository metadata | `commit: none` for Git initialization itself | Canonical root and exact operation |
| Backlog init at `planning/backlog` | Create documented native files/configuration | Policy: automatic exact-path planning commit; native init commit effect: uninspected | Installed init operation, payload, paths and effects |
| OpenSpec init at `planning/openspec` | Create documented native files/configuration | Policy: automatic exact-path planning commit; native init commit effect: uninspected | Installed init operation, payload, paths and effects |
| Beads init at sibling `beads/` | Create documented Beads/Dolt state | Native Dolt policy/effect: uninspected; no planning Git commit | Installed init operation, payload and effects |

These are symbolic root labels, not approval-ready literal targets. Adapt rows
to observed facts and selected owners; include missing dependencies as separate
rows when applicable. The automatic planning policy is conditional on a clean
isolated mutation and no approved `commit: none`, as defined in
[operations.md](operations.md). Inspect native init's result: if it already
committed, the policy does not add a duplicate commit.

Close the outline with final live status after setup. When that check becomes
the current action, read [diagnostics.md](diagnostics.md) and report the facts
observed then. The initialization preview itself describes that future check.
State that each mutation executes only under authorization covering its exact
preview. The current action below carries the actual review and authorization
check.

The checklist is an ephemeral preview, not SpeciFlow state, a queue, or
authorization for all steps. Reinspect native state after each approved item
and update the remaining preview. Apply the ownership approval boundary to
each concrete item, including applicable prior authorization. When approval
is missing, ask only for the current item; when
multiple owner installations are equally valid, present them as an
`ambiguous` choice.

## Current action

Finish the reply with the immediate next action. When inspection is complete,
show its exact preview, determine review from the documented effects, and
state whether existing authorization covers it or approval is needed before
execution. Uninspected details of later items do not gate a fully known
current item. Before requesting current approval, show the observed literal
source and target, documented operation, exact payload or configuration,
filesystem and native-state effects, and the applicable commit behavior. For
storage preparation, copy the literal `metadata_path` and the complete
three-field `expected_metadata` JSON returned by `storage.py resolve`.

If any required current-item fact is unavailable, the current response must
request no mutation approval, including conditional invitations or approval
for future execution. Do not label the current item approval-ready or claim
exact values were shown. State the missing fact and next in-scope read-only
inspection; perform it without extra approval when possible. If inspection is
unavailable or prohibited, report that limitation without asking mutation
approval.

Use the [effect-evidence review decision](operations.md#semantic-review) in
operations for initialization as for other native mutations.

A same-owner native init and conditional commit of its exact resulting paths
may be one previewed action and one approval. Inspect the init result before
committing; if the native tool already committed, do not add another commit.
Never combine different owners, dependency installation, storage preparation,
or planning Git init under that native-owner approval.
