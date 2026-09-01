# Task 4 protected descriptors and EffectLedger report

## Scope and threat-model disposition

Task 4 adds the serialized security boundary for protected native interrupts and
the minimal durable ledger for external-attempt facts. The implementation follows
the Local unsandboxed single-user profile: serialized descriptor/result values,
native coordinates, SQLite concurrency, and lease fencing are validated at their
real boundaries. Internal frozen values are not hardened against reflection or
arbitrary code already executing inside the TCB.

The ledger does not store workflow status, topology, routes, branch/join state,
terminal state, checkpoint payloads, timers, scheduler entries, or work items.
LangGraph checkpoints remain the sole workflow-state authority.

## RED evidence

The initial focused run failed with `21 failed, 8 errors`: the protected-effect
package and ledger did not exist. Subsequent RED cycles independently reproduced:

- unknown/dynamic descriptor fields, bool-as-int deadlines, unsafe write paths,
  unknown state selectors, oversized/deep values, and digest mismatches;
- mixed/unknown EffectResult and ScopeResult variants;
- stale CAS revisions and conflicting results with byte-identical rows after
  rejection;
- a stale effect worker crossing `prepared -> launching` after the durable lease
  had advanced to a new owner/epoch;
- forged scope digest/kind/runner selector/runner binding and wrong result kind;
- scope rows entering launch phases;
- managed `prepared -> PASS` without any external attempt;
- identical recovery preparation conflicting after lease adoption; and
- an active result sealing without the prepared runner binding;
- public `seal()` storing the reserved `launch_indeterminate` result under the
  ordinary `sealed` phase; and
- a NUL-bearing declared write path reaching canonical descriptor admission.

Each failure was observed before its production fix.

## Descriptor and result boundary

`parse_effect_descriptor` admits only `lockstep.effect/v1` and the closed known
effect kinds. It bounds the JSON domain before canonical encoding, rejects
unknown fields/capabilities/selectors, callable/non-JSON values, numeric
coercions, unsafe POSIX writes, Git controls, globs, duplicate declarations, and
NUL bytes, plus unknown graph-state selectors when the declared state set is
supplied.

Canonical JSON uses sorted object keys, declaration-ordered arrays, UTF-8, and no
insignificant whitespace. The SHA-256 digest therefore binds runner selection,
capabilities, input provenance selectors, writes, artifact contracts, deadline
and scope selectors. `effect_id` is a domain-separated digest of the exact public
native coordinate plus descriptor digest; it is correlation data, not a
credential.

Ordinary effect results and call/parallel scope results are separate closed,
discriminated schemas. Scope PASS binds the exact descriptor digest and, for a
call, the owner-resolved runner selector and binding digest. Scope ERROR is only
`scope_timeout` and cannot carry fabricated deadline/runner fields. Scope and
ordinary deadlines use the minimum of every active ancestor and the effect's or
scope's own duration. Unmanaged manual effects cannot declare deadlines or join
bounded scopes.

## External-attempt ledger

`storage.py` remains the sole SQL table owner. The main effect row contains only
the design's coordinate, digest, kind, immutable deadline/bindings/workspace,
phase, result refs/error code, timestamps, and CAS revision. A separate
append-only observation table retains phase/result facts without routing
authority.

The legal lifecycle is monotonic:

- ordinary attempt: `prepared -> launching -> running -> sealed -> delivered`;
- definite pre-launch failure: `prepared -> sealed -> delivered`, restricted to
  closed pre-launch ERROR codes;
- manual/scope no-spawn completion: `prepared -> sealed -> delivered`;
- ambiguous launch: `launching -> indeterminate -> delivered`, sealing exactly
  `ERROR/launch_indeterminate` and never permitting relaunch. Public `seal()`
  rejects that reserved error code from every phase; only `mark_indeterminate()`
  can construct and store it.

Every active-attempt transition verifies the current effect lease's owner, epoch,
scope/key, and expiry in the same `BEGIN IMMEDIATE` transaction as the revision
CAS. Lease renewal/adoption may advance the row's inspection epoch but never
authorizes a second launch. Scope rows cannot enter launch phases. Sealing checks
the exact result kind, effect ID, descriptor/scope digest, scope kind, runner
selector and runner binding. Replaying the same canonical result is idempotent;
a different result conflicts without row or phase mutation. Re-running identical
preparation adopts the existing record in every lifecycle phase.

No timer, scheduler, branch, or work-item table was added. Deadline wakeups and
provider reconciliation remain later coordinator responsibilities.

## Independent review

The independent review first found stale lease epochs and incomplete scope
binding. A second pass found invalid managed pre-launch success and recovery
preparation treating the mutable lease epoch as immutable. Each finding received
a focused RED regression and was fixed at the SQLite/boundary CAS. A final
self-review added mandatory runner-binding verification for active seals.

That independent pass returned **ZERO**. A subsequent external review found the
reserved ambiguity-result and NUL-path boundary omissions above. Both now have
RED/GREEN regressions that compare effect rows and append-only observations on
rejection. External re-review of `c11d568` returned **ZERO**: no remaining Task 4
P0, P1, or P2 findings.

## Verification

Final evidence after all review fixes:

- Focused descriptors, ledger, and no-legacy architecture guard:
  `41 passed in 1.27s`.
- Complete offline repository suite: `528 passed in 89.71s`.
- Ruff format/check over every Task 4 production and test file:
  `All checks passed!`.
- `compileall` and `git diff --check`: clean.

The two packaging tests that spawn nested `uv` processes were run with
`UV_OFFLINE=1` and the local dependency cache; no network access was used. No
push, publication, or upstream mutation was performed.
