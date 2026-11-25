# Reviewed package composition

Choose the parent and children by UUID from `atlas.list`. Use `atlas.status` or
`history` to choose the exact child **publication** you reviewed. Submit
`packages.configure` with the complete desired child list. For example, the
following HTTP request body places one published child under `field/materials`
in a parent that has no READY publication. Replace the example UUIDs and OID
with your own.

```json
{
  "atlas_id": "644c0451-e55f-4d23-ad32-b594903b7a29",
  "request_id": "b397ff3d-a8c8-47e1-bb6c-b42eb1627cd1",
  "expected_publication": null,
  "payload": {
    "includes": [{
      "package_id": "18facda8-9153-4a21-9072-2c3d254743c1",
      "mount": "field/materials",
      "publication": "8e469a13a58734debb0ce436d04b922c73a71190"
    }]
  }
}
```

For a published parent, supply its current READY OID in `expected_publication`.
After polling the operation, inspect its `candidate` with `candidate.inspect`.
Both results present `composition_diff`, with added and removed mounts and
before/after values for changed pins. Compare the UUID, receipt and content
commit. Removing a child means omitting the inclusion. Moving one means using
its new mount. An empty `includes` list clears all child inclusions. Each
configuration produces one candidate and preserves the parent's own material.

Accept or reject this candidate with `candidate.decide`. Acceptance uses the
existing reviewed publication flow. If the parent advances, inspect the new
READY, regenerate the layout against it, then review again. An identical
request UUID and body retrieves the original operation, even after child READY
advances. Use a fresh request UUID when changing the layout. To repin or roll
back a child, select its desired newer or earlier publication in the complete
list and review the new parent candidate.

Each configured child contributes its complete recursively pinned content. The
parent retains immutable per-mount receipt and content OIDs in Git and reserves
the corresponding objects in their owning repositories while the parent is
pending review or retained in history. For example, calling a mount
`causes/materials` assigns a directory path. Semantic relations and joint or
alternative grounds still come from the reviewed records. Repeated paths to
one child content version carry each receipt while contributing its evidence once.

The manifest, gitlinks and `.gitmodules` record the same layout. For a child
with a registered Git origin the submodule URL uses that locator. A child with
only a local repository uses a locator relative to the parent repository and
requires that directory layout for ordinary recursive Git checkout. Atlas
resolves registered children locally without checking out submodules. Historical
layouts that predate per-inclusion receipts retain their original content pins.

A recursive Git clone needs access to every recorded locator and pinned content
commit. For recovery on another machine, an Atlas full-history archive carries
the exact selected child publication objects as well. Restore maps their UUIDs
to the recovered repositories, so earlier and current layouts can be read after
the original locations disappear.

## Content-pin interface

Knowledge Atlas uses PoC9's exact Git submodule pins. An inclusion identifies a
child package by UUID and full commit OID, with a relative mount such as
`shared/materials`. The initial scope is the complete package (`all`). Branch names
and moving tags are not accepted pins.

Submit `packages.pin` with the common atlas ID, request UUID and expected READY,
plus this payload (replace the example values with actual IDs and paths):

```json
{
  "path": "/external/data/materials",
  "package_id": "child-package-uuid",
  "commit": "full-child-commit-oid",
  "mount": "shared/materials",
  "recorded_at": "2026-11-13T10:00:00Z"
}
```

The result is a pending candidate. Inspect it with `candidate.inspect`, assess
changed evidence and grounds, then explicitly accept or reject through
`candidate.decide`. A first package can be included in an empty atlas. Add an
optional `candidate` OID when composing further changes into an existing proposal;
its reviewed base must still match current READY.

To update, submit a new exact child OID at the same mount and review the resulting
candidate. To roll back, submit the earlier OID at that mount and review again.
Both create new candidates/publications; neither rewrites the previous publication
or automatically accepts the child change. A child advancing independently cannot
change the parent's selected knowledge.

`packages.inspect` uses current READY, an exact `publication`, or `as_known_at` and
returns the selected content, recursive package UUIDs, commit OIDs, resolved
locations and all inclusion paths. `candidate.inspect` exposes unpublished pins.
No manual submodule commands are needed for pinning, updating or rollback.

Repeated and diamond inclusion of the same UUID/OID preserve every mount while
contributing each evidence identity once. Cross-package joint grounds require all
members; independent alternatives survive withdrawal of another branch. Package
identity remains its UUID when its directory moves. The retained
`package-locations.json` mapping resolves moved package UUIDs to local Git roots;
archive restore reconstructs locations without redefining canonical identity.

Missing commits/packages, UUID mismatch, cycles, overlapping mounts and conflicting
simultaneous versions are explicit `invalid_package` errors. Resolve a conflict
before submitting a new pin; the handler never silently chooses a version or
follows a branch. Earlier published closures remain required for historical reads
and portable archives, even when the current pin changes.

Deterministic tests use the retained synthetic printlab material and PoC9 package
assertions. Native publication/search and composed archive recovery remain part of
the packaged lifecycle qualification; these checks do not establish human review.
