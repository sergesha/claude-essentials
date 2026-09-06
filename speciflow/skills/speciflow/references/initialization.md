# Initialization preview

Show the complete setup outline and the immediate next action. Use a compact
form that makes the operations, targets, expected effects, commit behavior and
missing inspection understandable; no fixed layout is required.

## Complete setup outline

For explicit setup or full-stack initialization, inspect the selected owners
and show the complete ordered outline before asking for the first approval.
Include applicable dependency installation, storage initialization, planning
Git initialization, each selected native-owner initialization with its commit
effect, and a final live status check. Keep different owners as separate
actions.

Storage `resolve` is a read-only path/metadata query; `init` creates the
directory and metadata. Do not substitute resolution for initialization.
Use the project-specific `data_root` and native-root locations from
[storage.md](storage.md) and [operations.md](operations.md). Unobserved paths
remain symbolic, not approval-ready literal targets.

Show known commit policy even when native effects remain uninspected. The
automatic planning policy is conditional on a clean isolated mutation and no
approved `commit: none`, as defined in operations. If native init already
committed, do not add a duplicate commit. Beads retains its native Dolt policy.

The outline is an ephemeral preview, not SpeciFlow state, a queue, or
authorization for all steps. Reinspect native state after each approved item
and update the remaining outline, retaining the final live check. Apply the
ownership approval boundary to each action, including applicable prior
authorization. If approval is missing, ask only for the current item. If
multiple owner installations are equally valid, present an `ambiguous` choice.

The final live check reports observations obtained after setup using
[diagnostics.md](diagnostics.md). If the user also asks for current status,
report the current evidence through that same reference; a future check does
not replace the requested current report.

## Current action

Finish with the immediate next action. When inspection is complete, show its
exact preview, determine review from the documented effects, and state whether
existing authorization covers it or approval is needed before execution.
Uninspected details of later items do not gate a fully known current item.
Before requesting approval, show the observed literal source and target,
documented operation, exact payload or configuration, filesystem and
native-state effects, and applicable commit behavior. For storage preparation,
copy the literal `metadata_path` and complete three-field `expected_metadata`
JSON returned by `storage.py resolve`.

If a required current-item fact is missing, state what is missing and the
next in-scope read-only inspection. Perform that inspection without extra
approval when permitted and available. If prohibited or unavailable, say why
it remains pending; do not imply it ran. In this missing-evidence response,
ask no approval question for a future write or read, and do not claim that
exact values were shown or the mutation is approval-ready.

Use the [effect-evidence review decision](operations.md#semantic-review) for
initialization as for other native mutations.

A same-owner native init and conditional commit of its exact resulting paths
may be one previewed action and one approval. Inspect the init result before
committing; if the native tool already committed, do not add another commit.
Never combine different owners, dependency installation, storage preparation,
or planning Git init under that native-owner approval.
