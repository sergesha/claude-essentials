# Task 12A canonical/start recovery GREEN

## Result

- RED: `03544bd`.
- GREEN: `d9045de`.
- No publication, push, issue, or PR occurred.

## Boundary

- `absent`: one write-free optimistic capture+plan, accepted only after a
  second read-only absence check;
- `ready`: recovery and the complete capture+plan run under the existing
  persistent project authoring lock;
- `initializing`: fail closed before canonical ingress without reader-side
  repair or owner-state creation;
- `absent -> ready`: discard optimistic success or failure and repeat the whole
  operation under the ready lock.

The selected `AuthorizedStartPlan` is immutable and is not replanned before
persistence. Existing runtime-policy currentness remains the final write fence.
MCP now uses the same `LockstepCommandService.start` driver as CLI/service use.

## Verification

- focused canonical matrix: `14 passed`;
- admission/MCP regression matrix: `111 passed`;
- authoring/recovery/CLI/MCP matrix: `131 passed`;
- final full engine suite: `1452 passed, 1 skipped`;
- `compileall` and `git diff --check`: clean;
- threat, reliability/concurrency, and architecture/SRP reviews: PASS with
  Critical/Important/Minor all zero.

The one full-suite warning is the unchanged reviewed 80-line cohesion warning
for `EffectCoordinator._reconcile_publication`.

## Next

Execute corrected Task 12A microcycle 2: replace the remaining direct
`write_compilation` and public CLI/MCP `recipe init` writer path with explicit
owner state and the shared authoring transaction.
