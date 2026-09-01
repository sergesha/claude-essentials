# Task 10 immutable artifact provenance and recoverable publication report

## Progress ledger

1. **Independent pre-code review:** the threat model, native design, Task 9
   report, and Task 10 plan were reviewed before the implementation was fixed.
   The reviews froze compiler-owned exact artifact sources, immutable producer
   sets, explicit accept/publish descriptors, commitment-first recovery, and a
   project-resolved publisher journal. Both pre-code verdicts were **GO**.
2. **RED:** exact producer/result/name/media bindings, child export bridges,
   registry crash visibility, authority/revocation cuts, destination collision
   and mutation, rollback modes, aggregate limits, restart, lifecycle races,
   multi-artifact producers, and directory durability were encoded as tests.
3. **GREEN:** Task 2 blobs/snapshots now back one immutable provenance registry;
   accepted artifacts lower to native publish interrupts; the coordinator owns
   the external publication effect and a bounded recoverable journal; service
   composition resolves each publisher from the exact project binding.
4. **Adversarial review rounds:** independent reviewers found and closed
   producer-set partial visibility, self-asserted validator provenance,
   lifecycle/unbind races, first-write revocation, leaf TOCTOU, whole-set
   verification, rollback modes, aggregate-preimage bounds, and parent-directory
   durability. Both final independent reviews returned **ZERO** on diff
   `afa5da69fb8a4d33867dbb9dd467308e1b60065859f10c3a57e8f2f6f175673d`.

## Architecture delivered

`ArtifactRegistry` publishes content-addressed manifests over existing
`BlobStore` and `ProjectSnapshotStore` objects. Every reference binds the exact
public run, project, definition, producer effect/request/workspace, full native
coordinate, descriptor, declared name/source/media, snapshot, and blob. A
producer-set commit marker is the sole visibility boundary: crash debris before
the marker is unreachable, retries are idempotent, and collisions fail closed.
Immutable file and directory namespace mutations are fsynced through the reused
owner-state boundary.

Child artifact contracts bind an exact logical producer, result state key,
declared name and media type. Lowering proves those bindings against the compiled
child descriptor, preserves the complete ordered declaration set (including
unexported declarations), and emits a native `call -> accept -> publish` bridge.
No child runtime, global artifact scan, mutable reference, or workflow-status
copy was introduced.

Acceptance is a session-owner operation over one exact pending native accept
interrupt and an exact registry reference. Validator content checks receive the
expected producer binding only through the engine-owned validation context;
graph state may select an `ArtifactRef` but cannot assert its provenance.
Content verification remains a separate destination-byte check.

`ProjectPublisher` is a distinct project-resolved port. Its closed journal binds
the exact request, grant/authority commitment, project root identity, ordered
destinations, artifact images, ancestor identities, modes, and rollback
preimages. Collision preflight is side-effect free. Applying and rollback each
advance one bounded monotonic action per reconcile, recheck the leaf at the
mutation edge, fsync file and directory changes, verify the whole set before a
terminal receipt, and recover from crashes without re-resolving authority after
the first committed replacement. Desired plus preimage bytes share a hard
aggregate bound.

All GraphRuntime app uses serialize with unbind/close under the existing
invocation guard. At the service boundary, foreground start, worker resume,
artifact acceptance, and background recovery additionally share one composite
adoption lock so recovery cannot unbind between guarded native calls. This adds
no scheduler, binding refcount, authority store, or duplicate workflow state.

## Verification evidence

- Focused artifact/publication/coordinator/storage set: `91 passed`.
- Expanded Task 10 set: `242 passed` before the final durability-only delta;
  independent final matrices: `172 passed, 3 deselected` and `57 passed`.
- Complete available suite: `827 passed, 1 skipped, 2 deselected` in `99.70s`.
  The two deselections are installer black-box tests whose isolated `uv
  sync`/`uv build` commands require unavailable DNS to fetch `hatchling`; all
  other dependency-patch tests ran.
- `python -m compileall -q src tests`: clean.
- `git diff --check`: clean.
- Ruff is unavailable: no Ruff executable or project dependency exists in the
  offline environment.
- Independent architecture/conformance reviews: **ZERO** on the exact frozen
  production diff.

Local commits: `f2778a9` (publication primitives) and `5edba5d` (native
integration and all review fixes). No network access, dependency mutation,
push, GitHub operation, external publication, fork, secondary artifact store,
or direct DSL/compiler filesystem publication was performed.
