# Task 12 owner-issued publication consent report

## Progress ledger

1. **Normative inputs:** implementation followed
   `/private/tmp/task12-consent-rebuild-plan.md`, `docs/DESIGN.md`, the Task 10
   and Task 11 reports, and `task-12-nonconsent-report.md`. The mandatory first
   RED correction made `receipt_digest` required in the committed acceptance
   fixture before any consent production code was added.
2. **Seven RED boundaries:** acceptance closure initially reported `9 failed,
   3 passed`; lowering failed on the new compiler-owned destination; CLI
   reported `4 failed, 12 passed`; MCP reported `4 failed, 27 passed`; the new
   SQLite authority, coordinator crash/race cases, and service controls each
   failed at their absent seam before their minimal production slice.
3. **GREEN integration:** exact owner commitments, SHA-256-only bearer-token
   storage, atomic/idempotent receipts, project epochs, token-only acceptance,
   and publication rechecks now compose through the existing effect authority
   and Task 10 commitment guard. The focused consent + Task 10 publication and
   crash matrix is `171 passed`.
4. **Authoritative-surface migration:** stale pre-consent acceptance/schema
   fixtures, the old 11-tool MCP set, and the old Codex interface-copy
   assertion were migrated to the current closed contract. No exact-surface
   test remains deselected.
5. **Self-review:** the threat stop-rule retained only reachable authority,
   correctness, and confidentiality deltas. It found and fixed one bounded
   CLI edge (a valid 4096-byte piped token plus its newline was rejected) and
   removed a duplicate registry read. No session authority, workflow state,
   second database, consent event stream, token vault, or applying-journal
   reauthorization was introduced.

## Architecture delivered

`OwnerConsentAuthority` is the single combined authority. It wraps the existing
non-publication delegate unchanged and owns two additive tables in the existing
owner SQLite database: project-scoped `consent_epochs` and exact
`publication_consents`. Issuance occurs only behind the local interactive CLI
preview/confirmation path. The database stores the raw token's SHA-256, never
the token; duplicate issuance for one project/epoch/commitment is rejected.

The commitment binds run, resolved project, definition, full native coordinate,
accept effect/descriptor, producer effect, artifact ref/blob digest, and the
compiler-owned destination, identity transformation, and local-project
audience. Redemption uses `BEGIN IMMEDIATE`; wrong or stale tokens and every
exact-field mismatch perform no write. First success stores one UTC timestamp
and canonical receipt digest. Sequential, crash, and two-thread retries return
the identical `AcceptanceResult`, whose receipt digest is required and whose
publication fields must match the `AcceptDescriptor`.

The service exposes preview/issue/revoke locally and acceptance as exactly
`token` plus keyword-only ambient project. Acceptance does not take a run,
step, artifact, consent reference, generation, runner, or session identity and
never calls `sessions.locked_owner`; the service admission lock still covers
redemption, native delivery, and engine-owned drive. The CLI has interactive
`consent issue` and `consent revoke`, hidden or one-line `consent accept`, and
no token argv. MCP registers only token-required `scenario_accept_artifact`;
there is no preview/issue/revoke/inspection tool and no origin/session call.

Publication intent carries each exact receipt. `resolve()` derives a
deterministic current-epoch publication grant from redeemed rows. Initial
mutation holds effect and publication leases plus the native commitment guard;
inside them, `commitment()` holds SQLite `BEGIN IMMEDIATE`, re-derives the exact
grant/request/current rows, and retains the writer lock while Task 10 durably
enters `applying` and attempts its first replacement. Revoke-first denies with
the journal still `prepared` and project bytes unchanged. Commit-first blocks
revocation until `applying` is durable. Journals already at `applying` or later
recover through the existing Task 10 branch without any consent read or second
authority call.

Revocation advances only the resolved project's epoch and retains old rows as
stale evidence. It cannot retarget a token and does not cancel an already
committed publication. README documentation states the exact ritual and the
honest residual: the interactive local owner boundary is not cryptographic
protection from a hostile process running as the same OS user.

## Verification evidence

- Acceptance commitment: `12 passed`.
- Owner SQLite authority/store, including raw-token DB-byte absence and both
  race orders: `8 passed`.
- Lowering and child bridge: `33 passed`.
- Coordinator: `39 passed`; service controls: `25 passed`.
- CLI after self-review cases: `18 passed`; MCP: `31 passed`.
- Consent + Task 10 publication/crash integration: `171 passed in 11.43s`.
- Bounded broad regression: runtime effects `132`, providers `56`, core
  runtime `225`, workflow `208`, and CLI/MCP/recipe authoring `56`, all passed.
- Final complete locked offline suite, including dependency patch/build
  black-box tests and migrated exact surfaces: `943 passed, 1 skipped in
  156.89s`; zero failures and zero deselections. The single skip is the
  existing environment-dependent integration skip.
- `python -m compileall -q src tests`: clean.
- `git diff --check`: clean.
- Ruff was unavailable in the locked environment (`Failed to spawn: ruff`);
  dependencies were not changed to add it.
- Code-review-graph change detection reported 19 changed tracked files at
  review time, risk `0.55`, and no affected registered flows; its syntactic
  test-gap mapping did not associate the direct CLI/MCP tests, so the focused
  and full executable evidence above remained authoritative.

No network access, dependency mutation, push, external publication, or
destructive repository operation was performed. The local commit is created
only after this report, the final GREEN suite, and the self-review.
