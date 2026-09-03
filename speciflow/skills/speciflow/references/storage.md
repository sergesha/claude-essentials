# Storage selection and bare initialization

SpeciFlow v1 supports POSIX hosts (macOS and Linux). Its sibling
`scripts/storage.py` is a private helper, not a public CLI and not an owner of
native tool data.

## Storage resolution

Resolve storage in this one exact order and use the first eligible result:

1. Explicit storage base selected for this invocation.
2. Nearest eligible project or ancestor `.speciflow` base.
3. Verified Git-common locator for the current worktree.
4. Standard POSIX home default `~/.speciflow`.

The helper uses the standard POSIX home resolution supplied by Python.
Missing, relative, or unverifiable home data leaves the
default selection unknown and non-mutating; an earlier eligible base remains
usable.

The project anchor is the verified canonical Git common directory in Git, with
the canonical worktree root reported separately. Outside Git, it is the
canonical invocation directory. A project key is lowercase SHA-256 over
`os.fsencode(str(canonical_anchor))`. Every base is a container: new project
data is `projects/<project-key>` and its base locator is
`locators/<project-key>/locator-v1.json`. Keep projects separated inside a shared store by native project identity or path structure.

Linked worktrees share their Git-common anchor; independent clones do not. A
non-Git project-local base is discoverable from a child only through its direct
`anchor-locator-v1.json`. An unmarked shared ancestor is ambiguous unless the
caller explicitly names the project anchor. The account default is the final
default candidate, not an unmarked ancestor.

Locators are UTF-8 JSON with exactly `version`, `anchor`, `project_key`,
`storage_base`, and `data_root`; version is `1`. Symlinks, duplicate or unknown
fields, copied/stale identities, locator mismatches, and one-target partial
locator state are conflicts. A Git-common or non-Git anchor locator used for
discovery must exactly equal its storage-base locator. Locator sharing controls
creation only; it never relaxes validation of an existing discovery locator.

## Bare init

`speciflow init` is an explicit natural-language or skill intent handled by the agent, not a shell command or installed CLI.

Bare init is only: resolve → exact storage preview → explicit approval → private helper init.
It does not select an owner, initialize a native tool or Git/Dolt, or commit.
The preview contains the selected anchor, base, data root, complete
locator contents, and every directory or locator file the helper would create.
The helper re-resolves immediately before mutation, compares compact sorted
UTF-8 preview JSON bytes, and creates only those approved local locator and
data-root paths. It creates no `planning/` or `beads/` directory. Exact repeat
is a no-op; changed facts, partial state, races, collisions, and symlinks are
conflicts.

After bare init, report that storage is ready and invite the user's product or
work request. Do not expose a bootstrap checklist.

Native owner selection and initialization require later owner-scoped previews
and approvals. One such preview may combine a native init with a conditional
commit of exactly its resulting paths; bare storage init never includes either
effect.
Never automatically commit the `claude-essentials` skill source repository.

## Native-root boundary

Native roots are selected at skill level, never by this helper and never in a
locator or helper preview. The separate operations guidance recognizes only
the roots `planning`, `backlog`, `openspec`, and `beads`; bare init remains
storage-only.

## Metadata exception

The default is no SpeciFlow metadata. The locator is the sole storage exception:
it contains only a canonical location and deterministic local namespace, never
owner content, process state, status, priority, readiness, assignment,
recommendation, or history.
