# SDD ledger — plan: .superpowers/plans/2026-08-20-lockstep-langgraph-native.md

Persistent workspace: `<HOME>/Projects/pets/claude-essentials-worktrees/lockstep-workflow-dsl` on `feat/lockstep-workflow-dsl`.

Task 1–11: complete before ledger recovery; authoritative evidence remains in the plan and sibling task reports.
Task 12R1a: implementation present; final combined R1 review remains pending.
Task 12R1b-P: complete (independent architecture and behavior re-reviews clean; current gate 1171 passed, 1 skipped; exactly 29 frozen R2 RED outside the gate).

2026-08-29 Task 12C design authorization: the user explicitly selected and completed the reviewed `simplify-with-write` remediation, accepted its final C0/I0/M0 product/architecture/threat gates at `4674e43`, and issued `go` for Task 12C design work and the proposed phase order. That `go` did not approve the later written design, an implementation plan, production/test changes, publication, push, GitHub issue/PR, merge, tag, or release.

Ruling: resume the existing master-plan SDD workspace at `lockstep/.superpowers/sdd/2026-08-20-lockstep-langgraph-native`; the stock resolver again targets the unwritable outer monorepo directory, while the authoritative ledger and annexes already live under the inner project as required — cost if wrong: helper scripts require explicit inner artifact paths, but no worktree or plan data is moved or recreated.

Task 12C preflight started from `4674e43fa1ffef1b9013f29345b2c7934808131e`. The master Task 12C file list is historical/stale for already-created R1/R2/A surfaces, so no production/test edit or implementation plan is permitted until independent current-state, installed-product, Gate-D, product, architecture, and threat audits produce a corrected written design and the user explicitly approves that exact spec.

Ruling: the stock `sdd-workspace` resolver targets the outer monorepo root, but this plan and its established artifacts intentionally live in `lockstep/.superpowers`; continue in that existing project workspace and do not create, move, delete, or recreate a worktree or plan directory — using the outer location would violate the user's persistence and plan-location requirements — cost if wrong: SDD helper scripts need explicit inner paths rather than automatic discovery.

## Task 12 remaining-unit preflight scan

| Producer | Consumer | Shared interface/files | Finding / ruling |
| --- | --- | --- | --- |
| 12R1b-P | 12R1b-A | `owner_policy`, immutable snapshot, static index, owner lock | Compatible: A consumes the reviewed immutable snapshot and must not add provisioning behavior. |
| 12R1b-A | 12R1b-E | admission decision, bound requirement identity, command service first-write boundary | Compatible: A stops before dynamic provider resolution/spawn; E alone owns adapters and commitment. |
| 12R1b-A | 12R2a/R2 | admission serialization and first-write boundary versus run-drive watches | Compatible only if the 29 frozen R2 tests remain unchanged and RED for their recorded future oracles. |
| 12R1b-E | 12R2 | coordinator commitment and restart reconstruction | Compatible: E supplies exact authority checks; R2 supplies scheduling/watch policy only. |
| 12A | 12C | authoring planner/publisher then active-contract cutover | Compatible and causally later; no authoring production edit belongs in R1b-A. |

Ruling (superseded by the A0/A1 ruling below): every R1b-A subunit has its own independently reviewed tests-only RED freeze followed by its production GREEN. The sequence is A0 RED review → A0 GREEN review → A1 RED review → A1 GREEN review — cost if wrong: two extra local review cycles, but no behavior is prematurely encoded or tested at an impossible boundary.

Task 12R1b-A initial RED attempt: stopped with `NEEDS_CONTEXT`; no source or test change. Independent plan review verdict: `PLAN_DEFECT`.

Ruling: split R1b-A into A0 static-admission and A1 first-write-currentness stages. A0 has only two positive fully granted real `codex`/`pinned` REDs; A8/A9-A12 are GREEN safety controls. A16 is introduced only after A0 GREEN, when a deterministic barrier can prove real preflight completed before supported provisioning drift — this preserves the final authority model and removes forced early-common/false REDs — cost if wrong: A16 lands one reviewed subunit later, but no production currentness or provider behavior is allowed into A0.

Task 12R1b-A0 RED: minor (deferred): the cohesive 427-line focused test duplicates several provisioning/recipe builders from `test_owner_snapshot_provisioning.py`; final architecture review must decide whether a small shared test-support module materially reduces drift without creating a generic fixture framework.

Task 12R1b-A0 RED fix round 1/5: commit `0ef7db9`; four original review findings addressed (real public boundary, complete owner-state snapshot oracle, provider marker, exact A9 partition/grant proof). One new Important remained because A10 crossed into native continuation.

Task 12R1b-A0 RED fix round 2/5: commit `d11eee5`; A10 narrowed to the real static `RuntimeRequirementIndex` boundary, with dynamic `OwnerConsentAuthority` behavior left to its existing service/effects owners. Focused evidence `2` intended RED / `5` GREEN; expanded Gate A `2` intended RED / `206` GREEN; compileall and diff check clean. Independent scoped re-review: PASS, no Critical or Important findings. RED freeze is complete; A0 production GREEN is next.

Task 12R1b-A0 GREEN: initial production commit `b9b11fd`. Review fix round 1 added honest restart/snapshot-integrity RED `d56bb5b` and GREEN `094c8ea`; both original Important addressed. Round 2 added repeated-classification RED `53c8389` and bounded positive-cache GREEN `5688ae4`; hashing Important addressed. Round 3 added teardown-order/test-cleanup RED `6149a6f` and GREEN `93bea35`; teardown Important and both test Minors addressed. Final independent verdict PASS with no Critical/Important/Minor. Final evidence: focused close/pump/recovery+A0 `36 passed`; Gate A `211 passed`; complete current suite excluding only frozen R2 `1182 passed, 1 skipped, 1 known warning`; architecture `20 passed, 1` unchanged warning; frozen R2 exactly `29` intentional failures; compileall/diff clean. A0 is complete; A1 RED freeze is next.

Task 12R1b-A1 RED freeze: initial tests-only commit `6cfef01`; review fix `6a61857` proves through `open_runtime_snapshot` before barrier release that supported revoke changed digest, advanced policy generation exactly once, preserved configuration/bindings, and removed the exact singleton grant. Independent scoped re-review PASS with no new findings. Evidence: A1+A0 `1` intended RED / `10` GREEN; Gate A `211` GREEN; frozen R2 exactly `29` intentional failures; compileall/diff clean. A1 production GREEN is next.

Task 12R1b-A1 GREEN: production commits `3d410b8`, `c511313`, and `92b946f` serialize exact decision currentness with the first durable start write while releasing the owner lock before unrelated recovery and blocking activation waits; a post-persist recovery failure reports the already committed park instead of a false failed start. Tests-only cleanup hardening ended at `f320e12`: handles register before start, registered-but-unstarted calls clean safely, every false stop is named and reported after all cleanup, original failures remain primary, and close is unconditional. Final independent re-review PASS with no Critical/Important/Minor. Evidence: direct cleanup `3 passed`; focused A1/A0/lifecycle `46 passed`; Gate A `219 passed`; architecture `20 passed` with the unchanged warning; complete current suite excluding R2 `1190 passed, 1 skipped, 1 known warning`; frozen R2 exactly `29` intentional failures; compileall/diff clean. A1 is complete; R1b-E tests-only RED freeze is next.

Task 12R1b-E RED preflight ruling: independent reproduction proved a plan defect in the old A5 wording. Installed `reviewed-change` compiles to one scope and `parallel-review` to two call scopes plus one parallel scope; all have parsed runner `None`, both inventories contain zero runtime requirements, and the coordinator produces no grant/request. They therefore remain GREEN public-command no-requirement/no-grant/no-spawn controls. A5 digest/adapter/request RED uses a separate real protected managed closure. Changing packaged templates to authority-bearing behavior is deferred to a separately reviewed Task 12C product-contract RED. The setup-failing draft was deleted and no code change retained.

Task 12R1b-E A5 RED freeze: corrected plan commit `5184d21`; initial tests-only freeze `cc22984`; commitment-boundary fix `cb537d8`; repeated durable reconstruction/observer extraction fix `6e2a249`. Final independent review PASS with no Critical/Important/Minor. Two packaged-template GREEN controls prove exact compiled scope counts with no requirement/grant/provider/owner-runtime authority while deliberately not freezing transient completion errors. The separate real managed Codex public path proves supported provisioning and exact static grant digest, then remains the sole intentional RED at the missing durable owner-current launch commitment before `ensure_started`; repeated bind/prepare reconstruction is correlated by exact effect/request/launch digests and runner binding. Evidence: focused `2 passed, 1` intended RED; Gate A `219 passed`; architecture `20 passed, 1` unchanged warning; frozen R2 exactly the same `29` intentional failures; compileall/diff clean. A5 RED is complete; A6/A7/A15/A16 tests-only RED freeze is next.

Task 12R1b-E staging correction: the attempted A6/A7/A15/A16 preflight made no file changes and independently confirmed a plan defect. Every protected public start still receives a `RuntimeAdmissionDecision` and unconditionally parks at E0; restart preserves the same park, while command composition still has unavailable authority and no configured runner. Later tests therefore cannot reach distinct lifecycle/restart/drift oracles and direct-adapter tests would duplicate existing provider coverage while bypassing the public contract. R1b-E is split into E0 A5 RED→GREEN, E1 A6+A7 RED→GREEN, E2 A15 RED→GREEN, and E3 A16 RED→GREEN, each independently reviewed. A5 RED is complete; A5 production GREEN is next.

Task 12R1b-E0 Gate A transition ruling: the first production preflight stopped with a clean worktree because A5 GREEN and four A0 park/cache tests require incompatible outcomes for the same granted public start; A1 wording also treats the transitional park as final. Independent audit confirmed PLAN_DEFECT. Before production, replace the two positive selector park tests one-for-one with real static planning/preflight proofs, remove only the two park-only restart/cache cases, preserve every negative A0 safety invariant and every A1 lock/currentness/committed-run invariant, and include A5 in the evolved gate. Expected pre-production evidence is `220 collected = 219 GREEN + 1` sole A5 commitment RED; after E0 it is `220 passed`. Historical Gate A `219 passed` remains A1-stage evidence, not an immutable count. E0 production remains stopped until this tests-only transition receives independent PASS.

Task 12R1b-E0 Gate A transition complete: tests-only commit `04ae933` replaced the two positive selector park cases with exact supported static planning/preflight proofs, removed only the two park-only restart/cache cases, and preserved all negative A0 plus all five A1 invariants. Independent review PASS with no Critical/Important/Minor. Fresh evidence: exactly `220` collected; `219 passed` with sole A5 RED at final `observer.reached`; exact same `29` frozen R2 failures; architecture `20 passed` with the unchanged warning; compileall/diff clean. E0 A5 production GREEN is unblocked and active.

Task 12R1b-E0 A5 GREEN complete: tests/design corrections `b2554c4`, `4d32412`, `74c3de1`, `61b78c9`, `2251200`, and `931bff4` removed the defective fixture assumptions and transitional cache/recovery seams while preserving the exact threat-model boundary. Production commit `23f82a7` binds immutable-bundle requirement reconstruction, exact owner grant/request/prepared-launch commitment under the shared owner snapshot lock, coherent first protected composition installation, and closed `codex`/`pinned` runners; no ambient enrichment, test-only seam, or partial authority/coordinator publication remains. Independent final review PASS with no Critical/Important/Minor. Final evidence: evolved Gate A `243 passed`; related coordinator/crash/parallel/provider controls `66 passed`; architecture `20 passed` with only the unchanged warning; full non-R2 `1191 passed, 1 skipped`; frozen R2 exactly the same `29` intentional failures; compileall and diff check clean. E0 is complete; E1 A6+A7 selector lifecycle RED freeze is next.

Task 12R1b-E1 preflight correction: independent reproduction returned `PLAN_DEFECT` only for the old two-RED staging. On `23f82a7`, A6 already reaches the real Codex executable, exact owner home, PASS/result/rollover, durable `delivered`, and native `completed`, so it becomes a carried GREEN control. A7's real authorized `kind=verify`, `selector=pinned` closure reaches exact grant/workspace/binding and durable `prepared`, then fails solely at `_PinnedCodexStrategy._validate_prepare_request` because it accepts only `kind=pinned`; no executable marker exists. Diagnostic `kind=pinned` proves the remaining pinned lifecycle GREEN with exact owner profile argv, credential-free home, PASS/no result/no snapshot, durable delivery, and native completion, but is forbidden as semantic masquerading. E1 now freezes sole A7 RED and must GREEN it through a closed allowed-kind contract without rewriting durable semantics or accepting arbitrary kinds.

Task 12R1b-E1 RED freeze complete: tests/design-only commit `2bfb994` carries A6 as a full real GREEN Codex lifecycle control and freezes A7 as the sole real public `verify → pinned` RED with exact final argv/home/no-publication/delivery/completion oracle. Independent final review PASS with no Critical/Important/Minor after correcting the last stale A6-as-RED sequencing sentence. Fresh evidence: E0 regression `4 passed`; focused E1 exactly `1 passed, 1` intended failure at `Codex adapter accepts only pinned effects`; A7 durable precondition is `effect_kind=verify`, `phase=prepared`, with exact grant/workspace/binding and no launch commitment or executable marker; compileall and diff check clean. E1 production GREEN is next.

Task 12R1b-E1 GREEN complete: production commit `d6bd2cb` replaces the single private driver kind with an immutable closed accepted-kind contract: base Codex exactly `{managed}`, pinned exactly `{pinned, verify}`. Validation is membership-only and never rewrites request, grant, or ledger semantics; direct controls prove honest verify preparation unchanged and managed rejection before attempt persistence/workspace/spawn. A7 now completes exact pinned argv/profile, full durable environment, credential-free home, PASS/no result/no snapshot, ledger `effect_kind=verify`/`delivered`, and native completion. Independent final review PASS with no Critical/Important/Minor. Fresh evidence: combined provider/E1 `38 passed`; E0 `4 passed`; architecture `20 passed` with unchanged warning; canonical Gate A `247 passed`; full non-R2 `1195 passed, 1 skipped, 1` unchanged warning; frozen R2 exactly the same `29` intentional failures; compileall and diff check clean. E1 is complete; E2 A15 restart reconstruction RED freeze is next.

Task 12R1b-E2 preflight correction: an external `/tmp` reproducer and independent review returned `PLAN_DEFECT` for the old rule that A15 could not fail at runner lookup. The real public path completes admission/grant/commitment, reaches durable `running`, starts exactly one production process held on an owner FIFO, and preserves valid catalog binding plus immutable bundle after live recipe deletion. Fresh reopen then fails closed in `_recover_effect_batch → _runner_for_binding` with `ProviderContractViolation: durably bound runner is unavailable for recovery` caused by `KeyError` of the exact ledger binding digest. Because fresh coordinator has no composition, this is the first honest oracle of missing restart composition reconstruction; avoiding it would require forbidden injected context or the production fix itself. A15 now freezes this sole RED, then requires immutable-bundle/current-owner reconstruction, atomic exact composition install, existing-process adoption, zero second spawn, PASS/result/rollover, delivery, and completion. No selector fallback, live recipe, regrant, owner drift/A16, or R2 policy is allowed.

Task 12R1b-E2 A15 RED freeze complete: tests/design-only commit `f43cb96` uses a real owner-provisioned managed run, production spawn held on an A15-specific FIFO, durable `running`, exact owner snapshot, catalog-bound immutable bundle, and deleted live recipe. Fresh public `scenario_recover` is the sole intended RED at the absent exact durable runner binding in `_recover_effect_batch → _runner_for_binding`. The final oracle requires atomic production composition reconstruction from immutable bundle/current owner state, exact ledger binding equality, fresh adapter spawn count zero, process adoption, PASS/result/rollover, durable delivery, native completion, unchanged catalog/bundle, and continued live-recipe absence. Cleanup cannot mask the primary RED. Independent final review PASS with no Critical/Important/Minor. Evidence: A15 one exact expected failure; E0/E1 `6 passed`; compileall and diff check clean. E2 production GREEN is next.

Task 12R1b-E2 GREEN complete: production now uses the focused `RuntimeExecutionRecovery` boundary to derive each selected automatic or explicit recovery page only from catalog-bound immutable bundles, then verify current owner snapshot/grants, every project boundary, closed provider kind, exact durable runner binding, and the `128 + 1` per-run capacity before reconciliation. Activation serialization installs at most one composition; later equal pages are still revalidated, differing contexts fail closed, and all admission paths preserve the global `activation → admission` order. A15 adopts the already-running production process with fresh adapter `spawn_count == 0`, completes PASS/result/rollover and durable/native delivery after the live recipe is deleted, and leaves catalog/bundle facts unchanged. Independent final review PASS with no Critical/Important/Minor after closing pagination, overflow, page-preflight, concurrent install, explicit recovery, binding drift, and consent lock-inversion findings. Evidence: focused final `41 passed`; Gate A `256 passed`; architecture `20 passed` with the unchanged warning; full non-R2 `1205 passed, 1 skipped, 1` unchanged warning; frozen R2 exactly the same `29` intentional failures; compileall/diff check clean. E2 is complete; E3/A16 RED freeze is next.

Task 12R1b-E3 complete as carried GREEN: independent `/tmp` reachability and review proved `PLAN_DEFECT` in the old RED staging. The exact A16 future contract was already GREEN from E0's shared owner-snapshot commitment guard. The tests-only barrier calls the original final `OwnerRuntimeEffectAuthority.resolve`, correlates its exact authority/intent/grant/request, and pauses before the original commitment. Supported CLI reprovision keeps the same selection key and policy generation while changing Codex binding/config generation and reissuing the grant. The unchanged commitment raises `_RuntimeAdmissionChanged` before `ensure_started`; the exact durable `launching` audit and passive native snapshot/pending coordinate stay unchanged, with zero spawn, markers, result, delivery, or native continuation. Independent final review PASS with no Critical/Important/Minor and no architecture finding. Evidence: focused `42 passed`; Gate A `258 passed`; architecture `20 passed` with unchanged warning; full non-R2 `1206 passed, 1 skipped, 1` unchanged warning; frozen R2 exactly the same `29` intentional failures; diff check clean. R1b-E0–E3 is complete; remaining Task 12 work and combined R1 review are next.
## 2026-08-27 — Task 12R1 combined completion

- Final reviewed range: `db1204f..31cca13`.
- Initial combined review: security PASS, behavior PASS, architecture I1/M1.
- Workspace explicit-dependency RED/GREEN: `2785cd6` / `91ffe90`.
- Owner module ownership/range hygiene cleanup: `38fe8dc`, `31cca13`.
- Final independent architecture, security/threat-model, and behavior reviews:
  PASS, C/I/M zero.
- Final evidence: Gate A `258 passed`; non-R2 `1207 passed, 1 skipped`, one
  unchanged reviewed warning; frozen R2 exact `29 failed`; compileall, rebuilt
  graph, status, working diff, and complete R1 range diff clean.
- Task 12R1 is complete. B0.4 native child→parent lineage is the active
  prerequisite discovered by the B0.5 feasibility probe; no R2 policy
  production has started.

## 2026-08-27 — B0.5 feasibility stop and B0.4 prerequisite

- A real public-service feasibility probe reached a managed child producer,
  durable delivery, exact registry materialization, and a real pending parent
  acceptance, but owner-consent preview failed first at native ancestry.
- Independent threat-model and architecture reviews confirmed a fail-closed
  Important functional defect, not an R2-watch failure and not a probe artifact.
  The native runtime cannot traverse the public parent history's exact completed
  subgraph snapshot to relate that producer to the later parent acceptance.
- B0.4 is inserted before B0.5: reviewed tests-only RED, minimal bounded native
  runtime/adapter GREEN, and independent range review. Coordinator acceptance
  checks and R2 policy remain untouched; B0.5/R2a stay stopped.

### B0.4 completion

- B0.4a port RED/GREEN: `7078588` / `3d1c282`.
- B0.4b behavior RED/GREEN: `674fe69` / `bc83324`.
- Real live/restart child→parent ancestry, exact mutations, sibling/fork,
  ambiguity, missing bridge, and aggregate ceiling controls: `41 passed`.
- Independent architecture and threat-model reviews: PASS, C/I/M zero.
- Architecture: `21 passed`, one unchanged reviewed warning. Frozen R2: exact
  same `29 failed`. Full non-R2: `1221 passed, 1 skipped`; the two nested-`uv`
  sandbox failures both passed on focused rerun outside the sandbox.
- Real B0.5 probe now reaches preview/issue consent and restart and fails only
  because the old dispatch watch is already absent. B0.4 is complete; B0.5
  tests/design RED freeze is next. No R2 policy production has started.

## 2026-08-27 — B0.5 real acceptance-lifetime RED frozen

- Commit `012fd10` adds the shared real compiled managed-child artifact fixture
  and the isolated acceptance-watch lifecycle test.
- Initial admission and reopen use `Engine.command`, production owner snapshot
  provisioning, real owner authority/composition, native GraphRuntime state,
  artifact registry, and owner consent. Only the exact-binding provider adapter
  and deterministic pump scheduling are substituted.
- Canonical focused evidence is `5 passed, 1 failed`; the sole intentional RED
  is the final retained-watch oracle, actual `()` versus `(run_id,)`.
- Independent threat-model and architecture reviews are PASS, C/I/M zero.
  R2a is next; no watch-lifetime or recovery policy production has started.

## 2026-08-27 — R2a/R2a.1 protocol and B1 policy freeze progress

- R2a protocol skeleton is complete through `3e226a8`: exact epoch-2 DTO/DDL,
  migration page protocol, ledger high-water/page/ack, and inert per-watch drive.
- R2a.1 neutral sweep reachability is complete through `374266d`, with its plan
  gate recorded at `9e7d29b`. Legacy B0 helper extraction is `e4dacc9`; the
  corrected master-plan milestone is `bce0a9a`.
- B1 item 11 tests-only RED: `2fc9af7`; real null-watch checkpoint/no-replay
  behavior, plus the no-checkpoint integrity control.
- B1 item 12 tests-only RED: `2474cb5`; real terminal cleanup busy/residue and
  pre-delete crash cuts with restart convergence.
- B1 item 13 tests-only RED: `d3c2bc3`; real restart-safe paged backfill past a
  128-row malformed prefix to one stranded Decision.
- B1 item 15 GREEN control: `2ebb991`; automatic and explicit recovery perform
  no migration-metadata SELECT/UPDATE and leave the incomplete metadata row
  byte-for-value unchanged.
- B1 item 16 tests-only RED: `b33fea5`; 129 real checkpointed worker parks and
  one later real Decision, two completed migration pages, and two complete
  concurrent admissions through a distinct SQLite connection after the first
  captured high-water. First-sweep exclusion/reachability and next-sweep
  visibility are one final composite RED at the inert sweep boundary.
- Fresh B1 evidence after item 16: exactly `4 failed, 23 passed` — items 11,
  12, 13, and 16 fail at their independently reviewed missing-policy oracles;
  item 15 and the protocol controls are GREEN. Item 16 focused setup is GREEN
  and its sole final assertion is RED; deterministic architecture analysis is
  `20 passed` with one unchanged reviewed production warning. Independent
  contract/threat-model and architecture/SRP reviews both PASS, C/I/M zero.
- No R2 behavior, Task 12A authoring behavior, Task 12C cutover, publication,
  push, GitHub issue, or PR has occurred. B1 item 17 writer-matrix freeze is
  next; item 14 remains correctly deferred until item 13 is GREEN.

Ruling: item 17's literal "every v2 command-side writer" includes every
independent production SQL transaction within its named categories. The matrix
therefore includes effect runtime-input facts, effect lease acquire/release,
and consent commitment in addition to ledger and consent row mutations. Catalog
and run-start facts remain part of atomic admission, while a later top-level
command reachability oracle owns pre-SQL immutable file staging — otherwise the
matrix would silently omit live writers or incorrectly forbid valid admission
staging. Cost if wrong: several extra RED nodeids and fencing edits during R2,
but no product behavior or public contract is broadened.

- B1 item 17a writer-fence matrix is frozen tests/design-only. Thirteen fresh
  per-case stores exercise every independently owned SQL transaction named by
  the ruling: atomic admission (including catalog and run-start facts), legacy
  command acknowledgement, already-fenced v2 acknowledgement and migration
  repair controls, effect prepare/transition/runtime-input/lease acquire and
  release, plus consent issue/redeem/revoke/publication commitment. Every case
  compares canonical logical rows and exact SQLite database/journal/WAL/SHM
  bytes; publication commitment additionally proves authority is not yielded.
  Focused evidence is exactly `11 failed, 2 passed`; combined B1 evidence is
  exactly `15 failed, 25 passed`, preserving the prior four policy REDs.
  Architecture analysis is `20 passed` with the one unchanged reviewed warning.
  Independent contract/threat-model and architecture/SRP reviews both PASS,
  C/I/M zero. Tests/design commit: `5846b90`. Production behavior remains
  unchanged; the minimal R2 item-13 GREEN microcycle is next.

- B1 item 13 GREEN is complete in `cdff78e`. Recovery now applies
  at most one 128-binding migration page per sweep with a 129th-row lookahead,
  resumes strictly from durable migration progress after a committed crash,
  and only then drives one bounded ordinary watch page. The initial ordinary
  slice handles exactly one genuine pending Decision through
  `EffectCoordinator.reconcile_one`; it does not replay input, clean terminal
  watches, wrap to later watch pages, or add writer fencing. Existing non-null
  v2 admission watches survive legacy classification without replacement.
  Missing/corrupt binding materialization is isolated as one malformed legacy
  run at the bind boundary; snapshot, SQL/lock, and coordinator failures remain
  observable. The item 13 fixture now includes a genuinely missing immutable
  bundle before the valid target and still proves cursor advancement and target
  completion. Item 11 became incidentally GREEN despite its missing positive
  replay oracle; that sequencing defect remains recorded for its own slice.
  The item 16 fixture was corrected to observe real v2 `RunDriveWatch` rows and
  again reaches its intended final multi-page/high-water RED. Fresh evidence:
  focused affected suites `71 passed, 1` expected item-12 RED; item 16 one
  expected final policy RED; architecture `21 passed` with the one unchanged
  reviewed warning; compileall and diff check clean. Final independent
  contract/threat-model and architecture/SRP reviews both PASS, C/I/M zero.
  Item 14 completion/no-rescan freeze is next after the item 13 commit.

- B1 item 14 is retained as an already-GREEN control in `34142cb`.
  Two real driver sweeps create durable completion through the exact target
  cursor; no protocol-seeded completion is used. The fixture then removes only
  the unrelated ordinary target watch through the real ledger acknowledgement
  API before taking its baseline, so the control does not freeze the still-RED
  item 12 watch-lifetime policy. Across two fresh reopen/sweep cycles, any
  catalog page, `_RunDriveBackfill` classification, migration-page apply, or
  native snapshot fails the test. Migration completion is read exactly once per
  reopen, while all logical owner rows and exact runtime SQLite database,
  journal, WAL, and SHM bytes remain unchanged. The deterministic SQLite image
  helper was mechanically extracted from the item 17 harness into one neutral
  two-consumer test helper; the writer matrix remains exactly `11 failed,
  2 passed`. Fresh evidence: affected item 13/14/protocol suites `31 passed`;
  item 14 `1 passed`; architecture `21 passed` with one unchanged reviewed
  warning; compileall and diff check clean. Independent contract/threat-model
  and architecture/SRP reviews both PASS, C/I/M zero.

- B1 item 15 post-item-13 extension is GREEN in `d90f8fe`. The
  original incomplete-row automatic/explicit recovery control remains intact;
  a second real population now completes through two driver sweeps and the
  exact target cursor, removes the unrelated ordinary target watch through the
  production ledger API, and proves the bounded watch page is empty before the
  observed window. A fresh command then exercises real automatic and explicit
  recovery with zero migration-metadata SELECT/UPDATE, the complete migration
  row unchanged, exact sweep routing, and an empty explicit result. The shared
  two-page real completion setup is now one cohesive fixture helper used by
  items 14 and 15. Fresh evidence: item 14 plus extended item 15 `2 passed`;
  affected suites `31 passed`; architecture `21 passed` with one unchanged
  reviewed warning; compileall and diff check clean. Independent
  contract/threat-model and architecture/SRP reviews both PASS, C/I/M zero.

- B1 item 11 positive replay contract is frozen tests-only in `f8c469a`.

## 2026-08-30 — Task 12C Task 1 analyzer ownership RED: baseline verified

- Execution start was clean `bcf944b`; focused architecture baseline was `151
  passed, 1` retained reviewed warning.
- Population/budget evidence is recorded in `task-12c-baseline.md`: 105
  tracked production Python files / 36,476 physical lines; 134 tracked test
  Python files / 40,277 physical lines; zero production and test deltas from
  normative budget baseline `4674e43`.
- The seven-role imports intentionally produce the correct collection RED at
  missing `architecture_candidate_policy`; no skeleton or production module
  was created.
- Controller correction: the incomplete output came from outer orchestration
  completing while its nested PTY remained live; one retry additionally leaked
  `UV_PROJECT_ENVIRONMENT` into nested `uv`. The isolated `bcf944b` baseline
  worktree with a `.venv` symlink and no leak completed `uv run --no-sync
  pytest -q` with `1708 passed, 1 skipped, 1 warning in 790.53s (0:13:10)`.
- The leak reproducer passed after cleanup: dependency-patches focused command
  `1 passed in 20.52s`. The baseline gate is unblocked for the independent RED
  review; no skeleton or production module was created.
- Fix round 1 closes analyzer one-role ownership: each role module must expose
  exactly its one required public top-level function, while public immutable
  record classes/types and private helpers remain permitted. Baseline evidence
  now records the exact NUL-safe count and `wc -l` commands yielding
  105/36,476 production and 134/40,277 test population facts.
- Task 12C Task 1 ownership GREEN: after the C0/I0/M0 RED review, seven
  test-owned analyzer skeletons were added with exactly their required public
  functions and `NotImplementedError` bodies only. They contain no analyzer
  behavior, rule data, production code, or internal role imports. Static
  compilation passed; the required focused ownership/import-direction command
  passed `3`, deselected `151` in `0.16s`. Full analyzer behavior remains
  deliberately unrun until the later role-specific tasks.
  A real start admission crashes immediately before its first native checkpoint,
  then closes the seed command. Two independent prepared reopen cycles each
  construct a fresh command/runtime/RecoveryDriver and re-read the same durable
  non-null `RunDriveWatch`. The oracle requires exactly one exact-BlobRef read
  and one `ensure_started(run_id, {})`, a durable checkpoint after the first
  recovery, an identical checkpoint after the second, retained watch identity,
  and no process-local replay memory. Focused evidence is the sole intended RED
  `len(reads) == 1`, observed `0`; the null-input control remains GREEN.
  Independent contract/threat-model and architecture/SRP reviews both PASS,
  C/I/M zero. Production remains unchanged; minimal item 11 GREEN is next.

- B1 item 11 GREEN is complete in `635be70`. `RecoveryDriver` now reads the
  public native snapshot first and replays only a no-checkpoint watch with a
  complete non-null BlobRef. Before the exact blob read it verifies the durable
  run-start snapshot binding, then uses the shared canonical start-input codec
  and the existing invocation-fenced idempotent `ensure_started`; any existing
  checkpoint suppresses blob/start work, and migrated null input remains
  safely blocked. The prior service codec was extracted without semantic or
  MCP import change. Fresh GREEN gate: `86 passed`; compileall and diff check
  clean. Item12 and item16 retain their intentional REDs. Final independent
  contract/threat-model and architecture/SRP reviews both PASS, C/I/M zero.

- B1 item 12 GREEN is complete in `f64f70f`. A watch observed at durable
  native terminal now runs bounded `reconcile_consumed`, remains present for
  every busy/deferred/non-delivered residual, and is acknowledged only after
  every residual is delivered or the bounded set is already empty. Reconcile
  and v2 delete are intentionally separate crash-safe transactions: the
  pre-delete cut retains the watch and a fresh reopen converges. A Decision
  delivery remains one bounded action and is never followed by same-call
  transient-terminal cleanup. Fresh gate: 7 focused functional controls plus
  20 architecture controls, `27 passed`; compileall and diff check clean.
  Independent contract/threat-model and architecture/SRP reviews both PASS,
  C/I/M zero. The pending-acceptance B0 RED remains correctly isolated to the
  later legacy acknowledgement cutover.

- B1 item 16 GREEN is complete in `94fdb75`. Each sweep captures one immutable
  admission-sequence high-water before its migration page, then scans bounded
  128-row pages through only that fixed population. Concurrent admissions and
  watches inserted by the current migration page are therefore deferred to the
  next sweep. The public `limit` counts accepted runnable drives rather than
  scanned worker parks, blocked rows, foreign projects, or terminal cleanup;
  cursor advancement remains monotonic across all of them. Terminal cleanup is
  explicitly non-attempting. A closed per-run integrity taxonomy reports only
  the public run ID and exception type, continues to later work, and deliberately
  leaves unknown, SQLite, schema, epoch, and page failures fail-closed. Fresh
  gate: `67 passed`; compileall and diff check clean. Three independent final
  feasibility, contract/security, and architecture/SRP reviews PASS, C/I/M
  zero. Item 17 epoch transition/writer fencing is next; no publication, push,
  GitHub issue, or PR occurred.

- B1 item 17 writer routing is GREEN in `83194b0`. The eleven remaining
  independent admission, legacy watch-delete, effect prepare/transition,
  runtime-input, lease acquire/release, and consent transaction owners now use
  the single existing `_v2_write_transaction` boundary. Together with the two
  already-fenced storage controls, the exact epoch-1 matrix is `13 passed` and
  proves rejection before logical/SQLite mutation and before publication
  authority is yielded. Normal epoch-2 affected evidence is `151 passed`; one
  separately known legacy null-input dispatch-reader cutover RED is explicitly
  deselected because the writer-only diff cannot reach it. Compileall and diff
  check are clean. Independent contract/security and architecture/SRP reviews
  PASS, C/I/M zero. Transition surface/behavior and command pre-open wiring
  remain separate subsequent microcycles; no transition code or publication
  occurred.

- B1 item 17b.0 transition surface is reserved in `7d61339`. The sole pre-open
  boundary is the exact private classmethod
  `RuntimeSchemaMigrator.transition_legacy_to_v2(path: Path) -> None`. Its
  staged implementation raises the exact `NotImplementedError` immediately and
  performs no filesystem, lock, SQLite, or schema I/O; no caller is wired.
  The dynamic surface/no-I/O control plus the writer matrix are `14 passed`;
  compileall and diff check clean. Independent contract/security and
  architecture/SRP reviews PASS, C/I/M zero. The real unwired transition
  behavioral RED freeze is next.

- B1 item 17b.1 transition-order behavior is frozen tests-only in `50ca96a`.
  One real three-process legacy-first scenario proves continuous ownership of
  `runtime-schema.lock`: the observer must first complete a timed contention
  attempt while the legacy `BEGIN IMMEDIATE` remains unreleasable, then it
  alone releases that writer and, at its first successful fence acquisition,
  requires an immediately available SQLite `BEGIN IMMEDIATE` plus the full
  committed v2 shape. This rejects missing, transient, or early-released
  fencing, including a release while the schema transaction is still live.
  The complementary stale process opens its SQLite connection before the
  transition, begins only after epoch 2, and must fail with an atomic rollback.
  The exact final state includes the migrated committed legacy watch, removed
  legacy table, epoch `(1, 2)`, positive DB-assigned sequence, and empty R2
  migration metadata. Focused evidence is exactly one surface PASS and one
  intended behavioral RED from the unchanged `NotImplementedError` stub;
  compileall and diff check are clean. Independent feasibility,
  contract/security, and architecture/SRP reviews all PASS, C/I/M zero.
  Production remains unchanged; the minimal unwired atomic transition GREEN is
  next.

Ruling: the first item 17b.1 GREEN attempt is rejected and remains uncommitted.
Its happy path proved the three-process ordering test, but independent review
demonstrated that table-name equality could promote same-name poisoned DDL,
valid v2 was not an idempotent no-op after a post-commit crash, and SQLite
family disposal/sealing happened after releasing the shared fence. The planned
absent/empty/v2/unknown/mixed/poison state matrix is therefore promoted ahead
of any production transition commit. Production was restored to the reviewed
stub before freezing that next RED. Cost if wrong: one larger transition
classification microcycle; benefit: no epoch-2 publication over untrusted DDL
and no ambiguous post-commit failure is admitted into history.

- B1 item 17b.2 transition state classification is frozen tests-only in
  `7ac1cfd`. Absent and zero-length stores are exact no-I/O/no-initialization
  cases; a production-created exact v2 store must be an idempotent byte-stable
  no-op, including retry of the transition's own committed legacy output.
  Seven noncanonical states must reject without changing logical rows or any
  SQLite database/journal/WAL/SHM byte: same-name wrong DDL in legacy and v2,
  mixed legacy plus the production epoch table, an extra view, an orphan legacy
  watch, epoch 1, and a missing epoch singleton. The rejection oracle accepts
  no staged `NotImplementedError` and freezes neither exception text/type nor
  generated SQLite DDL. Focused evidence is exactly `12 failed`, all at the
  unchanged staged boundary after successful fixture construction; compileall
  and diff check are clean, and architecture remains 21 PASS with the one
  unchanged reviewed warning. Independent feasibility, contract/security, and
  architecture/SRP reviews PASS, C/I/M zero. The exact classifier plus atomic
  transition GREEN is next.

- B1 item 17b.3 exact atomic transition GREEN is complete in `1e2e0ac`. The unwired
  pre-open transition now classifies only two finite product-owned states by
  their complete ordered raw `sqlite_master` manifests: the immediate legacy
  schema and exact v2. No SQL normalization or partial semantic parser remains;
  comments, quoting changes, generated columns, conflict policies,
  `AUTOINCREMENT`, extra objects, and any other DDL spelling difference are
  unknown state and reject write-free. Under the shared schema fence and one
  exclusive SQLite transaction, exact legacy rows are validated as a bounded
  stream, copied with `INSERT ... SELECT`, advanced to epoch 2, revalidated as
  exact v2, and committed. Exact v2 is an idempotent write-free retry; absent
  and zero-length stores remain untouched. The value gate preserves legitimate
  zero-byte inputs and rejects malformed or over-64-MiB references before DDL.
  Engine disposal plus SQLite-family verification/sealing remain inside the
  schema fence. Fresh evidence: transition surface/behavior/state `19 passed`;
  transition+migration affected gate `43 passed`; affected runtime plus
  architecture gate `58 passed` with the one unchanged reviewed cohesion
  warning; compileall and diff check clean. The broader legacy-watch suite
  retains its previously recorded null-input reader cutover RED (`16 failed,
  44 passed`), outside this unwired transition diff. Independent feasibility,
  contract/security, and architecture/SRP reviews all PASS, C/I/M zero. Command
  pre-open reachability remains the next separate microcycle; no wiring or
  publication occurred.

- B1 item 17b.4 command pre-open reachability is frozen tests-only in `b23d0e8`. A real
  public `Engine.command(...).scenario_recover(...)` path remains lazy at
  construction, then must call the exact transition once before the real
  `SQLiteStore` for legacy, zero-length, and exact-v2 databases. All three
  command cases reach the intended missing-routing RED; the legacy case also
  proves the current store rejects before accidentally blessing old state.
  Empty and v2 retain their successful command-open behavior once the missing
  call is supplied. A separate public projection `list_runs` control forbids
  any transition call and remains GREEN. The test deliberately freezes no
  private helper placement, SQLite sidecar bytes, transition internals, or
  broader recovery policy. Fresh focused evidence is exactly `3 failed,
  1 passed`; existing projection/service/public-composition controls are `130
  passed`; compileall and diff check are clean. Independent feasibility,
  contract/security, and architecture/SRP reviews all PASS, C/I/M zero.
  Production remains unchanged; minimal shared pre-store routing is next.

- B1 item 17b.5 command pre-open routing GREEN is complete in `a58527f`. The shared writable
  store opening boundary now resolves the exact owner database path, invokes
  `RuntimeSchemaMigrator.transition_legacy_to_v2`, and constructs the first
  `SQLiteStore` only after successful return. Construction remains lazy;
  projection remains structurally transition-free; all writable activation
  routes converge through the same store opening boundary. A transition failure
  occurs before `store` assignment, so existing activation rollback is safe and
  same-object retryable; committed-v2 retry remains idempotent. No migration,
  recovery, or policy logic moved into the service. Fresh evidence: command
  matrix `4 passed`; combined transition, writer, command, projection, service,
  and public-composition gate `166 passed`; architecture `21 passed` with the
  one unchanged reviewed cohesion warning; compileall and diff check clean.
  Independent feasibility, contract/security, and architecture/SRP reviews all
  PASS, C/I/M zero. No publication occurred.

- R2 final-cutover preflight: three independent reviews agree on one coherent
  cutover, not compatibility staging. Ruling: zero-byte non-null input remains
  valid because admission, BlobStore, and the exact schema transition already
  support it; the DTO contract is corrected from positive to non-negative,
  bounded at 64 MiB. Ruling: the existing ephemeral fresh-start exclusion is
  injected into the sole `RecoveryDriver` as a read-only predicate; an excluded
  row is a non-attempt and scan-continue, then becomes visible after the
  activation `finally` clears the exclusion. The tests-only surface reroute is
  next, followed by atomic removal of the legacy DTO/read/ack/recovery path.
  Cost if wrong: zero-byte admissions or first protected starts regress; the
  focused REDs must catch either before production commit.

- R2 final-cutover tests/design freeze is ready for commit. B0/B0.5 observers
  and actions use only v2 high-water/page plus the sole driver; historical
  acknowledged/no-watch state is explicitly distinct from the live retained
  Decision park. New executable contracts require dynamic service composition
  of fresh-admission exclusion, scan-continue without consuming the accepted
  limit, no EngineDriveService acknowledgement through either API, physical
  retirement of all six legacy lifecycle surfaces, v2 null-watch composition
  discovery, `admit_start -> RunDriveWatch`, and the inclusive zero..64 MiB
  input domain. Focused evidence: `90 passed, 29` intended RED; scoped replay
  correction `1 passed` with three exact cutover RED controls; compileall and
  diff check clean. Independent contract/security, feasibility/concurrency,
  and architecture/SRP re-reviews PASS, C/I/M zero. Production remains
  unchanged; one coherent GREEN cutover is next.

- R2 first GREEN diagnostic: focused production brought the frozen cutover
  from `29` RED to `7` RED (`112 passed`). Three independent reviews found no
  authority leak (all failures are fail-closed), but identified two blocking
  correctness gaps: each backfill page can insert watches above the correctly
  pre-captured high-water, migration incompleteness wrongly gated already-
  durable v2 work, and Decision-only recovery cannot advance a protected
  Scope/Effect with no ledger row. The corrected ruling makes the one bounded
  migration page independent of the original-high-water scan and adds only its
  exact committed migration-ID cohort; generic protected progress delegates
  through a run-id-only accepted-attempt port to `EngineDriveService`. The
  fairness trace of the retired
  `_drive_engine_owned` placement is removed while its semantic/native oracle
  remains. Production remains uncommitted until this correction is RED-frozen,
  GREEN, and independently reviewed.

- R2 final cutover complete locally: legacy dispatch-watch DTO/read/ack and the
  second service-side recovery loop/cursors are physically removed.
  `RecoveryDriver` is the sole replay and terminal watch-ack owner; generic
  protected progress crosses only the run-id accepted-attempt port. Fixed-H
  paging scans through irrelevant watches, exact migration IDs share the same
  accepted budget, and composition materialization stops at its protected
  bound. Atomic GraphRuntime binding ownership, own-only reservation rollback,
  service-owned binding handoff/release, and pump admission serialization close
  all independently reproduced lifecycle races while preserving foreground
  bindings and activation → admission order. Evidence: expanded gate `131
  passed`; targeted final regression `66 passed`; complete runtime `765 passed`;
  compileall and diff check clean. Independent contract/threat,
  feasibility/concurrency, and architecture/SRP reviews all PASS, C/I/M zero.
  No publication occurred. Next: commit this cutover, then perform the combined
  R1+R2 review before starting Task 12A or reassessing Task 13+.

- Combined R1+R2 review complete: independent cross-range analysis reproduced
  one real static-start/pump deadlock (`snapshot.lock → admission` versus
  `admission → snapshot.lock`). The reviewed fix moves the service's sole
  admission RLock into `_WritableCoreActivation` and enforces admission → current
  snapshot → nonblocking activation → persist; contention releases admission and
  snapshot before the blocking activation wait and retries currentness. Both an
  exact ordering oracle and a real two-thread regression are GREEN. Evidence:
  focused admission/commitment/recovery `94 passed`; complete project `1360
  passed, 1 skipped`, with the one existing reviewed architecture warning;
  compileall and diff check clean. Combined contract/threat,
  feasibility/concurrency, and architecture/SRP reviews all PASS, C/I/M zero.
  No publication occurred. Task 12A is next.

- Task 12A Gate C first tests-only microcycle complete: independent review
  rejected the original one-shot C1-C15 freeze because journal/fault/race tests
  would otherwise freeze guessed private seams or fail only at missing imports.
  The corrected staged gate now has two independent named-surface REDs, both
  packaged templates through CLI and MCP, public three-level compile and
  check/diff remediation, and an exact full output image including recipe,
  dependency manifest, source map, and every generated file. Parse, semantic,
  missing-child, and cycle planning failures remain exact write-free GREEN
  controls. Evidence: focused `8 failed, 4 passed`; existing template/recipe
  authoring `58 passed`; compileall and diff check clean. Contract and
  architecture/threat reviews PASS, C/I/M zero. No production file changed and
  no publication occurred. Next: commit this RED freeze, then make only the
  policy-free surface skeleton GREEN.

- Task 12A policy-free surface GREEN complete locally: immutable domain values
  now model one child-first source closure and paired exact destination images;
  path identities are stable `(path,dev,ino)` values, leaves carry raw regular-
  file mode/size/signed mtime, and planned after-images cannot pretend to own a
  post-replacement inode. `AuthoredRecipe` moved inward with identical public
  re-export identity. `AuthoringPublisher(state_dir)` is explicit, canonical,
  cwd-independent, and I/O-free; planner/publish/recover remain fail-closed.
  Evidence: Gate C `6 failed, 6 passed` (only intended behavior REDs), existing
  authoring `58 passed`, architecture `22 passed` with the existing warning,
  compileall/diff check clean. Contract/threat, feasibility, architecture/SRP
  reviews PASS, C/I/M zero. No publication occurred. Next: commit the surface,
  then freeze the reachable exact leaf-plan construction RED.

- Task 12A leaf RED correction: the leaf construction microcycle owns exactly
  the reachable recipe/manifest/source-map destinations; generated
  specialized-child outputs are deferred to the next direct-child planner RED,
  while whole-DAG Gate C retains its every-generated-output requirement. The
  leaf oracle now asserts the exact bundle type, mutates distinct project and
  source-ancestor path identities, and snapshots the complete `tmp_path` under
  controlled cwd/state environment to prove write-freedom. Evidence is recorded
  by the focused leaf and full authoring-bundle runs; production remains
  unchanged.

- Task 12A reachable leaf planner RED is review-clean and commit-ready. The
  exact return type, source/project/ancestor/leaf identities, three natural
  destination images, deep immutability, and complete ambient write-freedom
  are frozen. The snapshot records root and every descendant by `lstat`
  identity/change metadata plus exact regular bytes or raw symlink target;
  direct-child specialized outputs remain the next planner RED. Fresh evidence:
  focused leaf `1 failed` solely at the staged planner `NotImplementedError`;
  full Gate C `7 failed, 6 passed`; diff check clean. Independent contract/
  security, feasibility/portability, and architecture/SRP reviews all PASS,
  C/I/M zero. Production remains unchanged; tests-only commit is next.

- Task 12A reachable leaf planner RED committed as `1e81818`. The next
  microcycle is the minimal pure leaf planner GREEN; direct-child traversal,
  specialized generated outputs, publisher/journal behavior, and public
  adapter cutover remain out of scope until their own reviewed REDs.

- Task 12A pure leaf planner GREEN is review-clean. The planner captures each
  workflow byte sequence once, compiles that immutable capture, returns exact
  recipe/dependency/source-map before/after images, and performs no write or
  ambient state lookup. A per-plan stable real-directory identity cache binds
  project, source, and destination ancestors consistently; live, dangling,
  looping, swapped, and non-directory ancestors fail closed, true missing
  suffixes remain distinguishable, and paired images enforce one ancestor
  value set. Nonempty specialized generated outputs fail closed only until the
  next direct-child RED replaces that temporary leaf guard. Fresh final
  evidence: focused GREEN `10 passed`; Gate C exactly `6 failed, 15 passed`
  (the six deferred whole-DAG/publication behaviors); affected regressions
  `74 passed`; architecture `21 passed` with one unchanged reviewed warning;
  compileall and diff check clean. Independent contract/security,
  feasibility/concurrency, and architecture/SRP reviews all PASS, C/I/M zero.
  Production commit is next; no publication occurred.

- Task 12A pure leaf planner GREEN committed as `4c9660a`. The next microcycle
  is a tests/design-only direct-child RED that replaces the temporary
  generated-output rejection with natural whole-DAG reachability; publisher,
  journal, public adapter cutover, and template transaction behavior remain
  untouched.

- Task 12A direct-child planner RED replaces the temporary generated-file
  rejection with one natural public parent plan. Its child-first two-source
  contract derives every recipe, manifest, source map, and nonempty real
  specialized-child output from compiler results, while retaining exact
  source/directory identities and ambient write-freedom. The sole new node
  fails at `LSW304` because the current planner supplies an empty catalog and
  has no closure planning; leaf planner controls remain GREEN. No production,
  publisher, journal, recursive/transitive planner, CLI, or MCP behavior was
  changed. The direct-child planner GREEN is next.

- Task 12A direct-child planner RED is review-clean and commit-ready after one
  architecture fix round. The duplicate expected-compilation/source oracle was
  removed, and the direct-child slice now lives in a focused module with a
  short setup/action/composite-assert test plus cohesive helpers. Fresh
  evidence: the focused node fails exactly once at `LSW304`; leaf controls are
  `8 passed, 12 deselected`; current Gate C is exactly `7 failed, 122 passed`
  (the six previously deferred whole-DAG/publication cases plus this new RED);
  architecture is `21 passed` with the one unchanged reviewed warning;
  compileall and diff check are clean. Independent contract, feasibility/race,
  and architecture/SRP re-reviews all PASS, C/I/M zero. Production remains
  unchanged; a tests-only commit is next and no publication occurred.

- Task 12A direct-child planner RED committed as `88deff9`. The next
  microcycle is the minimal direct-child planner GREEN only; transitive
  traversal and publisher/journal behavior remain deferred to their own REDs.

- Task 12A minimal direct-child planner GREEN is review-clean and commit-ready.
  Parent and unique direct children are captured once with stable filesystem
  identities and compiled only from captured bytes. One cycle-free
  `authoring_compilation` owner now provides logical-name validation, call
  discovery, and canonical child-catalog construction to both the legacy
  authoring facade and bundle planner. The planner remains direct-child only;
  exact recipe/manifest/source-map/generated images and cross-role collision
  checks are projected without writes or ambient state access. Fresh evidence:
  focused `1 passed`; leaf controls `8 passed, 12 deselected`; current Gate C
  exactly `6 failed, 123 passed` (only the six previously deferred
  whole-DAG/publication cases); architecture `21 passed` with the one unchanged
  warning; compileall, RED-test immutability, and diff check clean. Independent
  contract, filesystem/TOCTOU/portability, and architecture/SRP reviews all
  PASS, C/I/M zero. Production commit is next; no publication occurred.

- Task 12A minimal direct-child planner GREEN committed as `ad81a65`. The next
  reviewed microcycle freezes the exact transitive child-first planner contract
  before adding recursion; publisher/journal/public adapters remain untouched.

- Task 12A transitive planner RED is review-clean after fix round 1. One natural
  `grandchild -> child -> parent` public plan freezes exact child-first sources,
  dependency edges, source/filesystem identities, complete per-role destination
  images including nonempty generated outputs, collision-free ownership, and
  full ambient write-freedom. The positive contract does not pin the temporary
  `LSW304` diagnostic; that remains observed RED evidence only. Generated-output
  classification has one shared test helper. Fresh evidence: direct-child
  control `1 passed`; transitive node fails exactly at current grandchild
  `LSW304`; current Gate C exactly `7 failed, 123 passed`; architecture `21
  passed` with the one unchanged warning; production diff, compileall, and diff
  check clean. Independent contract review PASS; architecture and feasibility
  findings were fixed and scoped re-reviews PASS, C/I/M zero. Tests-only commit
  is next; no publication occurred.

- Task 12A transitive planner RED committed as `65e0aac`. The next microcycle
  is the minimal pure transitive DAG planner GREEN; public adapter publication
  and publisher/journal behavior remain deferred.

- Task 12A pure transitive DAG planner GREEN is review-clean and commit-ready.
  The direct-only traversal became one deterministic DFS/postorder closure:
  each unique role is captured/compiled/projected once, shared dependencies
  reuse completed immutable results, cycles reject before a repeated capture,
  and every parent catalog contains only its completed direct children. The
  public planner stays thin and destination projection is unchanged; no write,
  ambient state, publisher, adapter, limit, or replacement behavior entered the
  slice. Fresh evidence: focused direct+transitive `2 passed`; leaf controls `8
  passed, 12 deselected`; current Gate C exactly `6 failed, 124 passed` (only
  the six deferred public whole-DAG/publication cases); architecture `21 passed`
  with one unchanged warning; compileall, committed-test immutability, and diff
  check clean. Independent contract, feasibility/TOCTOU/portability, and
  architecture/SRP reviews all PASS, C/I/M zero. Production commit is next; no
  publication occurred.

- Task 12A pure transitive DAG planner GREEN committed as `4773e5a`. Whole-DAG
  planning is now complete; the next reviewed microcycle freezes the reachable
  successful publisher transaction boundary before journal/fault/recovery/race
  tests. Public CLI/MCP cutover remains deferred until publisher semantics are
  proven.

- Task 12A successful publisher RED is review-clean and commit-ready. One
  public `AuthoringPublisher(state_dir).publish(bundle)` test freezes exact
  regular-file after bytes/modes, source identity preservation, an unrelated
  sentinel's exact integrity, and the absence of extra project files without
  naming journal paths, schemas, temporary files, or lifecycle hooks. Fresh
  evidence: focused `1 failed` solely at the unchanged public
  `NotImplementedError`; leaf planner control `1 passed`. Independent
  contract/threat, feasibility/portability, and architecture/SRP reviews all
  PASS after the destination `lstat` and sentinel fix, C/I/M zero. Production
  remains unchanged; a tests-only commit is next and no publication occurred.

- Task 12A successful publisher RED committed as `58d9f24`. The success-path
  transaction GREEN is review-clean after one substantive fix loop. The thin
  publisher now coordinates a project-bound owner lock/journal, bounded exact
  identity validation, root-anchored descriptor traversal, same-directory
  staging/no-clobber replacement, durability, final read-set validation, and
  exact owned rollback. Review found and the fix loop closed three important
  holes: untracked stage/directory cleanup could have removed recovery
  evidence, final-parent-only checking admitted an ancestor-symlink swap, and
  changed files could be read without a memory bound. Typed identities remain
  owned by the bundle and filesystem responsibilities are split coherently;
  all three re-reviews PASS, C/I/M zero. Fresh evidence: publisher/planner
  selection `17 passed, 6 failed`, with exactly the six previously frozen
  public CLI/MCP whole-DAG REDs; templates/recipe CLI `58 passed`; architecture
  `21 passed` with one unchanged warning; compileall, diff check, and committed
  publisher-test immutability exit zero. Crash recovery, fault/race matrices,
  limits, and adapter cutover remain separate next microcycles; no external
  publication occurred.

- Task 12A successful publisher transaction GREEN committed as `b49b002`.
  Source-race characterization coverage is review-clean: a child edit after
  planning is rejected before publication, and a source edit after each of the
  three real leaf destination mutation ordinals preserves the edit while
  restoring the exact all-old destination set and unrelated sentinel. Tests
  wrap only natural `linkat`/`renameat` boundaries filtered by destination leaf
  and parent descriptor identity; no private lifecycle hook or journal schema
  is frozen. All four new nodes fail honestly against the pre-GREEN `58d9f24`
  public stub and pass on current production. Fresh focused evidence is `5
  passed`; contract/threat, feasibility/portability, and architecture/SRP
  reviews all PASS, C/I/M zero. A tests-only regression commit is next; crash
  cuts, durable recovery, destination races, and limits remain deferred.

- Task 12A source-race regressions committed as `aa4eb73`. The next required
  publisher-fault dependency, exact present destination before-images, is now
  frozen as an honest planner RED. A legacy public compilation seeds real
  outputs, the workflow changes, and the write-free replan must bind exact old
  bytes/mode/full leaf identity/ancestors while projecting the exact new
  canonical image and excluding sources from the write set. The focused node
  fails solely at the explicit `existing compilation destinations are not yet
  supported` branch; leaf controls are `7 passed` and publisher controls `5
  passed`. After one architecture fix round, the main scenario is linear,
  publisher-independent, and uses one cohesive transition helper; all contract,
  feasibility, and architecture reviews PASS, C/I/M zero. Tests-only commit is
  next; no external publication occurred.

- Task 12A exact existing-destination planner RED committed as `a514b23`.
  Its GREEN is review-clean after one architecture fix loop. Planning now
  captures absent or exact present regular destinations with bounded no-follow
  reads, full leaf/mode/bytes identities, and ancestor revalidation. One shared
  `AuthoringBudget` owns identical count/per-file/aggregate limits for planner
  and publisher, and each role's projected outputs are admitted immediately
  after compilation before the next role is retained. Low-level capture-time
  observation moved to a narrow acyclic module; publish-time revalidation stays
  separate. Fresh planner/publisher/architecture evidence is `43 passed, 6
  deselected`, with only the six intentional public adapter REDs omitted and
  one unchanged architecture warning; compileall, diff check, and test-tree
  immutability exit zero. All three re-reviews PASS, C/I/M zero. Production
  commit is next; publisher fault/recovery matrices remain deferred.

- Task 12A exact existing-destination planner GREEN committed as `a529fa7`.
  The first real existing-output fault slice is now review-clean RED: the same
  public publisher creates an initial leaf bundle, an edited replan captures
  three exact present preimages, and an injected failure immediately after each
  successful destination `renameat` ordinal must restore all-old state. The
  syscall wrapper is filtered by destination leaf and parent descriptor, so it
  excludes journal/staging operations and freezes no private hook. Fresh
  evidence is exactly `3 failed`: for each ordinal only the selected destination
  remains new, proving the bookkeeping window after kernel replacement and
  before applied-ownership recording; existing publisher controls remain `5
  passed`. Contract, feasibility, and architecture reviews PASS after switching
  fixture seeding from the legacy writer to the same public publisher, C/I/M
  zero. Tests-only commit is next; no external publication occurred.

- Task 12A post-replacement rollback fault RED committed as `4302e13`. Its
  GREEN is review-clean after one ownership-proof fix loop. Destination
  ownership is reserved before the namespace syscall, then one verified-dirfd
  observation classifies exact before versus exact transaction after; rollback
  no-ops, restores/removes, or retains the journal on ambiguity accordingly.
  Stage bookkeeping now distinguishes attempted and proven consumption: a
  missing stage is accepted only after syscall success or exact-after proof,
  never from a speculative pre-mark. This closes post-effect exceptions without
  weakening pre-effect cleanup or foreign-inode preservation. Fresh evidence:
  focused fault matrix `3 passed`; combined publisher/planner/architecture `46
  passed, 6 deselected`, one unchanged warning; compileall, diff check, and
  committed-test immutability exit zero. Contract/threat, feasibility, and
  architecture/SRP re-reviews PASS, C/I/M zero. Production commit is next;
  broader fsync/journal crash cuts and autonomous recovery remain deferred.

- Task 12A ambiguous replacement-ownership GREEN committed as `8e55beb`. Public
  recovery after destination-rename crash cuts is now frozen as a review-clean
  RED. A custom `BaseException` after each real target `renameat` bypasses
  in-process rollback while releasing test-process descriptors/lock; before
  recovery the oracle proves the exact new-prefix/old-suffix mixed state and an
  opaque newly durable owner-only regular evidence file, without naming its
  path or schema. A fresh public publisher must restore all-old and a second
  recovery must be a no-op. Fresh evidence is exactly `3 failed`, all at the
  current public `AuthoringRecoveryRequired`; existing publisher controls are
  `8 passed`. Contract/threat, feasibility, and architecture reviews PASS after
  strengthening durable-residue evidence and concrete snapshot typing, C/I/M
  zero. Tests-only commit is next; no external publication occurred.

- Task 12A rename-crash recovery RED committed as `e75a1ff`. Its bounded GREEN
  is review-clean after one durability/idempotence fix loop. A strict typed,
  bounded, project-bound journal model feeds a descriptor-rooted recovery
  executor: exact desired-before has terminal precedence, exact planned-after
  is restored, foreign/ambiguous state retains evidence, complete forward and
  recovery stages are reconciled, every desired-before file/parent is re-fsynced,
  and journal absence is namespace-synced for retry after an unlink durability
  cut. `ctime_ns` now participates consistently in capture/journal/read
  stability. Any absent before-image is explicitly rejected before mutation,
  so created-directory recovery remains honestly deferred. Fresh evidence:
  publisher `11 passed`; planner/architecture `38 passed, 6 deselected`, one
  unchanged warning; compileall, diff check, and test immutability exit zero.
  All scoped re-reviews PASS, C/I/M zero. The next mandatory fault RED is
  incomplete forward/recovery stage creation or mid-write; current behavior
  there is safe fail-closed with journal and partial evidence preserved. No
  external publication occurred.

- Task 12A incomplete-stage crash RED committed as `83c71f8`. Four natural
  filesystem cuts freeze process death immediately after successful
  `O_CREAT|O_EXCL` and after a strict non-empty prefix write for both forward
  publication and restoration stages. The tests identify the calls by flags,
  destination-parent descriptors and created inode rather than private temp
  names/schema, preserve same-parent foreign siblings, and require exact
  all-before recovery plus two fresh-publisher idempotence. Review correction
  removed impossible directory timestamp equality and strengthened semantic
  namespace/file assertions. Fresh RED was exactly `4 failed`; prior publisher
  controls remained `11 passed`; all contract, feasibility and architecture
  reviews PASS, C/I/M zero.

- Task 12A whole-set stage-reservation RED committed as `ed60c2b` after the
  independent threat review exposed the otherwise unsafe `EEXIST` crash gap.
  Forward and restoration collisions at the final actual reserved write-set
  path must now fail before any stage create/write, destination replace, or
  durable owner-journal replace; the foreign inode/bytes/mode remain exact and
  two fresh recoveries are syscall-level no-ops. Paths are obtained through the
  production derivation with fixed natural randomness, not duplicated filename
  syntax. Fresh RED was exactly `2 failed`; the four prior REDs and eleven prior
  GREEN controls were unchanged. Three re-reviews PASS, C/I/M zero.

- Task 12A reserved-stage ownership and incomplete-stage recovery GREEN
  committed as `960b226`. Under the project authoring lock, one descriptor-rooted
  read-only preflight proves the complete forward/restoration reserved set
  absent before journal admission. A strict v2 journal durably binds the exact
  operation/write-set/path derivation; old, missing, false, partial, reordered
  or tampered evidence fails closed. Recovery explicitly classifies stages as
  absent/complete/incomplete, reclaims an incomplete forward stage only in an
  all-before state, and recycles an incomplete restoration stage only while its
  destination remains after, with same-inode recheck and parent fsync before
  exclusive recreation. Fresh evidence: publisher `17 passed`; planner gates
  `8 passed` plus direct-child `2 passed`; architecture `21 passed` with one
  unchanged warning; compileall and diff check clean. Independent contract/
  threat, filesystem/crash feasibility and architecture/SRP reviews all PASS,
  C/I/M zero, including malformed-journal, incompatible-phase, absent-parent,
  nonregular collision and fd-leak probes. The broader bundle module's six
  public adapter/canonical-diff REDs are baseline-identical and remain for the
  next planned Task 12A slice. No external publication occurred.

- Task 12A absent-destination-leaf crash RED committed as `9e3fd07`. With all
  destination parents pre-existing, process death immediately after each real
  no-clobber hard-link publication freezes every new-prefix/absent-suffix cut.
  The public recovery contract requires exact all-old absence, preservation of
  source, parent identities and foreign siblings, opaque durable owner evidence,
  and a second recovery with no mutation syscall. Review strengthened that
  no-op probe to cover write-capable opens, timestamp/truncation/removal and
  platform mutation calls while permitting only an opaquely discovered,
  non-destructive lock open. Fresh evidence is exactly `3 failed`, all at the
  explicit absent-before recovery rejection; publisher/direct-child controls
  are `19 passed` and stable planner contracts `11 passed`. Contract/threat,
  feasibility and architecture/SRP reviews PASS after the probe correction,
  C/I/M zero. The bounded recovery GREEN committed as `5ca56ee`: recovery now
  removes only an exactly revalidated transaction-created absent-before leaf,
  fsyncs its parent, and reconciles a surviving hard-link stage using the same
  device/inode plus exact planned hash/size/mode after the expected ctime
  change. Durable absence fsyncs the parent without opening the missing leaf.
  Real APFS probes and injected deaths after both unlink/fsync pairs all
  converged, a foreign non-alias stage failed before destination mutation, and
  FD count remained `4 -> 4`. Final evidence: combined focused/publisher/
  direct-child/architecture `43 passed` with one unchanged warning; planner
  `8 passed, 13 deselected`; compileall, diff check and frozen-test integrity
  clean. Three production re-reviews PASS, C/I/M zero. Created-directory
  ownership remains the next separate durability slice. No external
  publication occurred.

- Task 12A transaction-created-directory crash RED committed as `923f05b`.
  One naturally absent destination parent is frozen at four real syscall cuts:
  before `mkdir`, immediately after `mkdir`, after the pre-existing parent
  fsync, and after the first destination hard-link. Planned absence identifies
  only a candidate; automatic removal requires an atomic journal replacement
  carrying exact directory identity and a parent fsync. The opaque WAL oracle
  is generation-bound, accepts only current non-empty owner-only evidence, and
  invalidates every older durability proof on a later replacement. An empty
  different-inode replacement and a foreign child are preserved across two
  mutation-free failed recoveries. Shared crash snapshot/mutation mechanics
  moved to `_authoring_crash_gate.py`; the prior absent-leaf test shrank by 175
  lines and remains green. Fresh characterization: exactly `3 failed, 6
  passed`; publisher/direct-child/architecture `40 passed` with one unchanged
  warning; planner `8 passed, 13 deselected`; compileall, production
  immutability and staged diff checks clean. Contract/threat and architecture/
  SRP re-reviews PASS after exact-cut, full-image, active-evidence and WAL
  generation fixes, C/I/M zero. The bounded journal/recovery GREEN is next; no
  external publication occurred.

- Task 12A transaction-created-directory recovery GREEN committed as
  `8fb0dac`. New journal writes use strict v3 monotonic directory identity
  progress derived only from validated write paths/ancestor chains; strict v2
  remains readable as empty progress without inferred ownership. Creation now
  orders `mkdir -> descriptor identity -> atomic owner journal replace/fsync ->
  project-parent fsync`. A dedicated 100-line directory recovery plan performs
  whole-set candidate/child preflight before leaf mutation, enrolls only exact
  journal-bound inodes, synthesizes absence below missing candidates, cleans
  leaves/stages first, then revalidates and removes directories deepest-first.
  Missing restart states fsync the nearest verified parent; replacements,
  unexpected children and unowned post-mkdir states retain the journal. Fresh
  evidence: focused `9 passed`; publisher/direct-child/architecture `40 passed`
  with one unchanged warning; planner `8 passed, 13 deselected`; compileall,
  diff check and committed-test immutability clean. Independent real crash probes
  after `rmdir` and after its parent fsync converged twice; strict-v2 absent/
  present probes respectively converged and failed closed. Contract/threat,
  filesystem/restart and architecture/SRP reviews PASS, C/I/M zero. No external
  publication occurred.

- Task 12A terminal-commit crash RED committed as `d4c7e42` and GREEN as
  `a01037b`. The corrected two-case oracle reaches the same all-new destination
  set plus the same durable source edit on opposite sides of final read-set
  validation, so neither full replacement progress nor recovery-time source
  matching can substitute for durable commit evidence. Strict journal v4 now
  records one boolean commit fact after final validation and outside the
  rollback boundary; v2/v3 remain uncommitted. A separate committed recovery
  policy preflights exact after-images and complete reserved-stage absence,
  rechecks/fsyncs each observed inode and parent, preserves later source edits
  and created directories, then durably retires the journal. Shared bounded
  observation/TOCTOU primitives were mechanically extracted; rollback recovery
  shrank and retained its behavior. Fresh final gates: focused plus adjacent
  crash/publisher/direct-child/architecture `51 passed` with one unchanged
  warning; planner `8 passed, 13 deselected`; compileall and diff check clean.
  Full engine suite was `1405 passed, 1 skipped, 6 failed`; the six child-closure
  rebuild REDs reproduce unchanged on exported clean base `d4c7e42` and remain
  subsequent Task 12 work. Three production reviews PASS, C/I/M zero. No
  external publication occurred.

- Task 12A public child-closure publication GREEN completed on base `a01037b`.
  CLI and MCP now run explicit same-project recovery, one complete closure
  planning pass, and one atomic publication of that exact immutable bundle;
  the MCP result is retained from the same pass. Invalid names remain
  write-free, while missing/manual/source resolution correctly follows Gate C
  recovery. The legacy direct writer remains only for the bounded init/test
  compatibility path. Evidence: six child-closure REDs `6 passed`; invalid-name
  boundary `32 passed`; expanded gate `161 passed`; full engine suite `1411
  passed, 1 skipped`, with one unchanged EffectCoordinator complexity warning;
  compileall and diff check clean. Three production re-reviews PASS, C/I/M
  zero. GREEN committed locally as `80febbf`. No external publication
  occurred.

- Task 12A read-side recovery and serialized observation completed. The
  recovery-order RED is `4247044`, exact-lock concurrency RED is `0ff1a70`,
  and GREEN is `e0e7cf0`. CLI/MCP named check and diff validate before any
  owner-state action; named and `check --all` then recover and derive their
  complete immutable result/error under the same deterministic project lock
  used by publication, including when no prior journal namespace exists. A real
  cooperating publisher proves exact lock inode contention and cannot mutate a
  destination before observation completion. Empty/traversal names remain
  write-free. Evidence: focused `15 passed`; publisher/API/CLI/MCP `126 passed`;
  full engine `1426 passed, 1 skipped`, with one unchanged reviewed complexity
  warning. Three final production reviews PASS, C/I/M zero. No external
  publication occurred. The template transaction RED is the next leaf.

- Task 12A template role and empty-read-set prerequisites completed. DTO RED
  `c66d36e` and GREEN `22c5e0c` make non-empty child-first topology the sole
  role owner, permit only complete ordinary sources or `sources == ()`, and
  reject partial inventories. Recovery RED `ff83164` uses a real publisher-
  created destination-only v4 journal and a post-link process-death cut; GREEN
  `26464e9` accepts its empty read set without changing the journal shape.
  v2/v3 and ordinary non-empty role membership remain strict. Evidence: DTO
  focused `4 passed`; expanded prerequisite `59 passed`; destination-only crash
  `4 passed`; publisher/recovery `36 passed`; all RED and production reviews
  PASS, C/I/M zero. No external publication occurred. The public CLI template
  transaction RED is the next leaf.

- Task 12A public template transaction cutover completed. Tests-only RED
  `d7ea9e2` freezes explicit external owner state, one recover→publish adapter,
  exact all-absent preflight, inert legacy project data, complete generated
  output coverage, and real post-link process-death recovery. Production GREEN
  `09ab0f4` removes the template-specific staging/journal/recovery writer,
  captures package bytes once, compiles the child-first role DAG, plans one
  destination-only immutable bundle, and routes it through the shared
  `AuthoringPublisher`. Evidence: transaction `7 passed`; template/recipe CLI
  `58 passed`; bundle/publisher `57 passed`; post-cleanup focused `65 passed`;
  full engine `1438 passed, 1 skipped`, with the same reviewed complexity
  warning. Three independent production reviews PASS, C/I/M zero; diff check
  clean. No external publication occurred. Next: perform the combined Task 12A
  authoring-range review before entering Task 12C Gate D.

- Task 12A first combined authoring-range review of `1a75172..09ab0f4` failed;
  Task 12A is not complete and Task 12C remains blocked. The template cutover
  itself is review-clean, but the range still lacks canonical/start recovery
  serialization, transactional public `recipe init`, one-plan result binding
  for check/diff/canonical, and frozen Gate C items 4/7/8/12/13/14 in their
  complete required matrices. Architecture/SRP review passed C/I/M zero; threat
  and reliability reviews found the missing authority/coverage boundaries.
  The corrected seven-microcycle dependency order is now embedded in the Task
  12 plan. No external publication occurred. The canonical/start tests-only RED
  is active next.

- Task 12A canonical/start recovery microcycle completed. The initial GREEN was
  discarded after the full suite reproduced a write-free lifecycle regression
  and independent review found an active-journal ABA gap. The replacement RED
  `03544bd` freezes the monotonic authoring boundary in all three states:
  absent, ready, and initializing; it also covers absent-to-ready transitions
  after both optimistic success and normalized failure. GREEN `d9045de` makes
  the persistent project lock the readiness fact, never creates it on the
  reader path, retries a complete capture+plan under that lock when a writer
  appears, and consumes exactly one selected immutable `AuthorizedStartPlan`.
  MCP delegates to the same command service. Runtime policy currentness remains
  fenced immediately before persistence. Evidence: canonical matrix `14
  passed`; admission/MCP `111 passed`; authoring/recovery/CLI/MCP `131 passed`;
  final full engine `1452 passed, 1 skipped`, with the same reviewed
  EffectCoordinator complexity warning; compileall and diff check clean. Three
  final production reviews PASS, C/I/M zero. No external publication occurred.
  Next: microcycle 2, cut `write_compilation` and public CLI/MCP `recipe init`
  over to explicit owner state plus the shared transaction.

- Task 12A transactional recipe initialization microcycle completed. Test-only
  RED commits `e3f49fa` and `34a6acc` freeze explicit keyword-only owner state,
  exact `recover → plan → publish` ordering, one destination-only minimal-init
  bundle, all-destination collision rejection, stable CLI/MCP outputs, and
  whole-DAG direct compilation. GREEN `9c040e5` adds a neutral captured-workflow
  planner, reduces template installation to a thin facade, removes direct
  project writes from `recipe init`/`write_compilation`, and routes both through
  the shared `AuthoringPublisher`. Evidence: focused `84 passed`; expanded
  authoring `168 passed`; packaging `2 passed`; final full engine `1458 passed,
  1 skipped`, with the same reviewed EffectCoordinator warning; compileall,
  import, and diff checks clean. Architecture, threat, and reliability reviews
  PASS C/I/M zero. No external publication occurred. Next: microcycle 3, bind
  check/diff/canonical to one captured whole-DAG plan/result.

- Task 12A one-plan result-binding microcycle completed. Tests-only RED
  `9b8a877` freezes one captured whole-DAG plan/result for check, diff,
  canonical matching, and public generated-recipe preflight. Production GREEN
  `a0cd158` adds a pure captured-result projector, removes the second recursive
  filesystem ingress, and reuses the same plan-owned candidate and proof. Final
  review fixes also route the bounded classification bytes through strict YAML
  limits and make the recovery oracle observe classification capture lock
  state. Evidence: focused `29 passed`; affected `368 passed`; architecture
  `21 passed` with the unchanged warning; full engine `1473 passed, 1 skipped`;
  compileall and diff checks clean. Architecture, threat, and reliability
  reviews PASS C/I/M zero. No external publication occurred. Next: microcycle
  4, freeze the complete Gate C item 4 planning/no-journal matrix and item 13
  project-binding/256-record/4-MiB owner-state bounds.

- Task 12A Gate C item 4/13 matrix completed as an all-GREEN tests-only freeze
  in `e5bc69f`. Three focused modules cover the public planning/no-journal
  failure matrix, direct publisher revalidation of 257 read and paired-write
  records plus independent 4-MiB read/before/after groups, and exact
  project-identity-bound recovery despite project-local redirect data. No
  production change was necessary. Evidence: new focused plus architecture
  `43 passed`; original bundle/publisher `57 passed`; diff check clean. Three
  independent reviews PASS C/I/M zero. Ruling: aggregate exhaustion surfaced
  through the bounded reader's per-file wording is not a product defect—the
  required overflow rejection, exact project write-freedom, and pre-journal
  behavior are already correct; changing only message taxonomy would add
  unrequired code. No external publication occurred. Next: microcycle 5, Gate
  C item 7 durability cuts and item 14 per-destination foreign edit/create
  oracles on shared real fault infrastructure.

- Task 12A Gate C item 7/14 microcycle completed. Tests-only commits `10203a8`
  and `cadc612` freeze an exact durability protocol trace, 24 crash cuts,
  foreign edit/create at all three destinations, and ordinary `EIO` before and
  after a real no-clobber link. The initial all-GREEN create interpretation was
  rejected by threat review: `EEXIST` proves no publication ownership and must
  not create permanent fail-closed residue. The honest RED was three create
  cases leaving 3/2/1 owned stages plus trusted journal evidence. GREEN
  `cb52a57` removes only the just-enrolled replacement record for definitive
  `FileExistsError`; generic errors and crash windows remain conservatively
  enrolled. Final evidence: new matrix `37 passed`; adjacent authoring/recovery
  `66 passed`; architecture `21 passed` with the unchanged warning; compileall
  and diff check clean. Three independent final reviews PASS C/I/M zero. No
  external publication occurred. Next: microcycle 6, Gate C item 8 durable
  neither-before-nor-after recovery.

- Task 12A Gate C item 8 foreign recovery preflight completed as carried-GREEN
  tests-only commit `6a27dec`; no production change was needed. After a real
  replace/link plus destination-parent fsync, or after the real durable
  committed marker, a third foreign inode is installed and two fresh recovery
  attempts must fail without any mutation while preserving exact project,
  owner, journal, stages, and foreign identity. The required matrix was bounded
  to nine structural equivalence cells after architecture/threat/reliability
  reconciliation; the remaining 15 Cartesian cells add no distinct reachable
  control flow. Parsed journal assertions prove exact committed/uncommitted
  phase and progress, preventing wrong-branch GREEN. Evidence: new `9 passed`;
  adjacent recovery/crash/architecture `91 passed`; compileall and diff check
  clean. Three final reviews PASS C/I/M zero. No external publication occurred.
  Next: microcycle 7, Gate C item 12 writer-vs-writer serialization.

- Task 12A Gate C item 12 cooperating-writer serialization completed as
  tests-only commit `f206016`; no production change was needed. Five
  proportional public-path cells cover overlapping replace/replace,
  distinguishable link/link, disjoint replace/link, queued recovery after a
  durable uncommitted crash, and the recovered/planned publish gap. Each cell
  proves real contention on the exact persistent project lock inode, exact
  acquire/release ordering, bounded cleanup of every started thread, and full
  project/owner terminal images. Thread-attributed canonical mutation probes
  prove stale losers are read-only, disjoint writes remain inside the lock, and
  crash evidence is exactly one new destination with the remaining stages and
  active journal. Evidence: focused `5 passed`; a no-op `fcntl.flock` control
  makes all `5 failed`; adjacent authoring `68 passed`; architecture controls
  retain only the unchanged reviewed warning; compileall and diff checks clean.
  Three independent final reviews PASS C/I/M zero. No external publication
  occurred. The corrected seven-microcycle sequence is complete. Next: repeat
  the combined Task 12A range review; Task 12C remains blocked.

- The second combined Task 12A range review did not close Task 12A. Threat
  review passed C/I/M zero; reliability found the three missing committed-marker
  crash cuts; architecture found a remaining public second whole-DAG planner,
  five TCB complexity hotspots, duplicated descriptor observation, and mixed
  module responsibilities. The full engine suite failed `14` tests after the
  existing 128-worker park test exhausted the macOS soft FD limit: every
  retained native app owns two SQLite descriptors, reaching 248 FDs at 120
  apps. A reduced-limit diagnostic with explicit unbind passed at the baseline
  FD count. Corrective microcycles 8–12 are now binding in the corrected replan;
  Task 12C and Gate P remain blocked.

- Task 12A corrective microcycle 8 completed. Tests-only RED `c403909` and
  GREEN `304f88a` replace retained parked apps with one scoped catalog-backed
  lifecycle while preserving exact crash handoff and bounded same-process
  recovery. The `RLIMIT_NOFILE=64` 128-run matrix stays bounded and later runs
  rebind/resume; causal controls cover pre-/post-handoff failures, periodic
  recovery, cancelled wakeups, cold activation/session fencing, public/private
  run indistinguishability, stale-session no-bind behavior, and concurrent
  owner/borrower exclusion. Evidence: affected `126 passed`; wheel/build `2
  passed`; full engine `1556 passed, 1 skipped`, with only the existing reviewed
  architecture warning; compileall and diff checks clean. Reliability and
  threat/architecture reviews PASS C/I/M zero. No external publication
  occurred. Next: corrective microcycle 9 committed-marker crash cuts.

- Task 12A corrective microcycle 9 completed. Tests-only REDs `df48b27` and
  `a70e0c8` plus GREEN `06184cd` freeze the three committed-marker syscall cuts,
  honest orphan-temp process-death evidence, bounded whole-set preflight before
  mutation, exact directory-durable retirement, and write-free repeated
  recovery. Evidence: focused `30 passed`; adjacent authoring `191 passed`;
  full engine `1561 passed, 1 skipped`, with only the unchanged reviewed
  architecture warning; compileall and diff checks clean. Reliability and
  threat/architecture reviews PASS C/I/M zero. No external publication
  occurred. Next: corrective microcycle 10 authoritative single-plan traversal.

- Task 12A corrective microcycle 10 completed. Tests-only RED `574667c` and
  GREEN `e073a49` delete the recursive public DAG planner and project direct
  compile plus workflow estimate from the same immutable plan already used by
  write/canonical/check/diff. Full public compile tuple and structurally distinct
  estimate mutations freeze same-pass behavior. Evidence: causal `2 passed`;
  related authoring `64 passed`; authoring+architecture `213 passed`; full
  engine `1563 passed, 1 skipped`, with only the unchanged reviewed warning;
  compileall/diff checks clean. Reliability and threat/architecture reviews
  PASS C/I/M zero. No external publication occurred. Next: corrective
  microcycle 11 TCB decomposition and test-module split.

- Task 12A corrective microcycle 11 completed. Tests-only RED `7cfa323` and
  GREEN `335ba2c` guard and decompose the five SRP-confirmed TCB hotspots,
  consolidate stable bounded no-follow descriptor reading without merging
  caller policy, and split the two mixed authoring test modules into explicit
  semantic owners. All 30 original tests remain exactly once; all new helpers
  are below the deterministic hard metrics, while cohesive DTO, cleanup, and
  recovery-parser units remain intentionally intact. Evidence: focused plus
  architecture `82 passed`; full engine `1568 passed, 1 skipped`, with only the
  unchanged reviewed warning; compileall/diff checks clean. Reliability and
  threat/architecture reviews PASS C/I/M zero. No external publication
  occurred. Next: corrective microcycle 12 final combined Task 12A verification
  and three independent range reviews.

- Task 12A corrective microcycle 12 completed and Task 12A closed. Final
  authoritative evidence: engine `1568 passed, 1 skipped`; compileall, clean
  worktree, and full-range `git diff --check 1a75172..932e4b0` clean. Independent
  threat-model, behavior/reliability, and architecture/SRP reviews of the full
  range all PASS C/I/M zero with no blockers or missing evidence. No external
  publication occurred. Next: blocking Task 12A.5 / Gate P quantitative
  complexity and proportionality audit, followed by explicit user selection of
  keep/simplify/redesign/stop before Task 12C may begin.

- Task 12A.5 / Gate P analysis and independent review completed at `97893e3`.
  The decision-grade audit is
  `.superpowers/reviews/2026-08-29-task-12a5-proportionality.md`. Exact current
  code population: production 117 files / 39,455 physical lines / 18,997 AST
  statements; tests 147 / 46,627 / 21,414. Authoring alone is 4,847 production
  and 8,843 test lines. The recommended bounded option is
  `simplify-with-write`: preserve automatic CLI/template creation and all
  threat-boundary controls while removing crash-atomic multi-file rollback and
  recovery, targeting 1,800–2,200 production and 2,300–3,200 test lines in at
  most 10 modules. `simplify-owner-applied-patch` is separately exposed because
  it changes the product contract. Global locking, runtime watch/publication,
  effects, authority, lowering, and CAS are outside the first range. After two
  correction rounds, independent product/proportionality, architecture/SRP,
  and threat-model reviews all PASS C/I/M zero. No production code or external
  publication changed. The sole blocker is explicit user selection among
  `keep`, `simplify-with-write`, `simplify-owner-applied-patch`,
  `redesign/re-scope`, or `stop`; any simplification requires a separately
  written and approved remediation plan before Task 12C.

- Task 12A simplify-with-write Tasks 1–8 and final fix round 1 completed locally
  in this evidence commit. Active authoring is now 8 production modules / 1,779
  lines (Gate P baseline 4,847; reduction 3,068) and 15 focused tests / 2,065
  lines (baseline 8,843; reduction 6,778). The corrected top-anchored measure for
  the five fixed integration consumers is 58 additions / 87 deletions / net
  -29 against `675c5bd`; inspection finds no new lifecycle/state/lock/
  persistence ownership. The active reliability boundary is exact:
  cooperating writers serialize; each file replacement is atomic and durable;
  a crash may leave old, new, or mixed generated files; runtime start admits
  only a freshly observed complete canonical closure and exact DAG. Repeated
  first-init/template installation completes only a strict proper canonical
  prefix with exact planned bytes/modes; full/holed/mismatched sets collide.
  Legacy refusal is exact-project; doctor adds bounded read-only discovery;
  legacy evidence requires a pre-simplification recovery build and must not be
  manually deleted. Fresh evidence: compileall exit 0; direct/adversarial 165
  passed; authoring-focused 189 passed; schema/architecture/installed/doctor
  333 passed with one pre-existing non-authoring warning; full engine 1,708
  passed, 1 skipped, the same warning. Task 12C remains
  blocked pending the parent-assigned independent final reviews; no publication
  occurred.

- Task 12A.5 / Gate P and the `simplify-with-write` remediation are now closed.
  Final fix round 1 is `be924c3829640b3f057cb6da0d8cc5eaece97974`; final
  evidence refresh is `4674e43fa1ffef1b9013f29345b2c7934808131e`.
  Independent product/proportionality, architecture/SRP, and
  threat/reliability reviews all returned PASS with Critical/Important/Minor
  zero. The user accepted that evidence and approved this exact Task 12C order:
  repository-wide architecture ratchet/remediation; DSL artifact export and
  authority-bearing built-in templates; complete tests-only Gate D RED;
  minimal installed-contract GREEN; then full source, wheel, and
  clean-installed reviews followed by a stop before publication. The current
  candidate is
  `.superpowers/specs/2026-08-29-task-12c-current-design.md`. Independent review
  of its first commit `a2637536c44668cf18597c5b2449eba40edc8392` returned
  Product C0/I7/M1, Architecture C2/I6/M0, and Threat C0/I5/M0; design fix round
  1 incorporates all named findings and adds a finding-to-section response
  matrix. Review of fix-round-1 commit
  `f2ae0182f44a17d95d24cb14810ee2d8c3f2ec12` left A-C1, A-C2, A-I3, A-I5,
  and Product N-I1. Design fix round 2 freezes separate domain/lifecycle
  bindings, canonical analyzer schemas/digests and committed-tree review
  evidence, Python/lambda/binding semantics, seven one-directional self-gated
  analyzer modules, and deterministic credential/network-free Task 12C
  acceptance. Actual credentialed/live scenarios remain conditional Task 13
  work after post-Task-12 roadmap reevaluation and separate user approval.
  Final architecture review of round-2 commit
  `43668ac27235ff83a09722f278f836801186d705` left A-C2 path-namespace and A-I3
  class-body-lambda attribution gaps. Design fix round 3 closes review evidence
  over distinct digest-bound inner-project/Git-tree paths and freezes exact
  function→class→file lambda attribution with class semantic/domain/lifecycle/
  mutation/cohesion evidence. If
  and only if the user explicitly approves the corrected written
  spec, it conditionally supersedes the master plan's stale Task 12C
  create-file, `recipe init --template`, and atomic full-bundle wording while
  preserving them as historical evidence. Re-review and user approval of the
  spec are next. No architecture RED, production/test change, implementation
  plan, publication, push, issue/PR, merge, tag, or release is authorized.

- Task 12C architecture analyzer, production lifecycle binding, and repository
  inventory RED are implemented on `feat/lockstep-workflow-dsl` through
  `2ed1d2b`. The seven test-owned analyzer roles pass their closed self-gates;
  the lifecycle table now contains 12 exact production bindings and leaves
  replay/no-op ambiguity fail-closed. Independent lifecycle GREEN review is
  C0/I0/M0. Exact repository composition covers 105 tracked production Python
  files with zero unresolved call or dependency evidence and emits 198 unique
  candidates (115 function, 46 one-hop, 12 class, 25 file). The canonical
  diagnostic was reproduced independently with SHA-256
  `fc8e9fdeab327051ce2b8bc9122f79a3831d4d7db75f6ce4e53423490ce86170`. The
  complete exact metrics, per-candidate adjudication, remediation DAG, focused gates,
  preserved invariants and threat-model feasibility are recorded in
  `.superpowers/reports/2026-08-30-task-12c-architecture-inventory.md`.
  Current adjudication is 58 mixed-responsibility candidates to remediate and
  140 cohesive candidates eligible for fresh post-remediation evidence; no
  semantic stop condition or prohibited new owner/schema/state/lock is present.
  Production remediation begins after independent final inventory review. No
  publication action occurred.
