# Reviewed package composition

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
