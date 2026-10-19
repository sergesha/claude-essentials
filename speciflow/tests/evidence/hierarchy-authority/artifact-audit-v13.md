# Independent v13 artifact audit

Audit date: 2026-10-19. Scope: saved process evidence, source integrity, recorded automated-test evidence, and score arithmetic/provenance. Production files were not modified. This audit did not begin with an assumed target score.

## Direct process evidence

Read `process-case.md` and `process-rubric-v2.md`, then examined actual `state.json` event streams and `approved-plan.md` artifacts for `process/candidate-v13-1..6` and `process/baseline-1..6`. Independently replayed prerequisites and confirmation state from events rather than accepting the existing inspection summaries.

| Trials | Original method loaded | Singleton eligible questions with recommendations | Frontier complete before confirmation | Correction and final confirmation | Correct authorized publication | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| candidate-v13-1..6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| baseline-1..6 | 6/6 | 6/6 | 4/6 | 6/6 eventually | 6/6 | 4/6 |

Every v13 trace contains nine ordered events: method load; format, filename, delimiter and date-clock questions; rejected UTC confirmation with local-time correction; newly enabled timezone question; positive final confirmation; publication. All five questions have recorded responses. All six plans agree on CSV, date filenames, semicolon, and local time in Europe/Berlin, and match their publication event content byte for byte.

Baseline trials 1 and 2 ask for confirmation at event index 4 (zero-based) while delimiter remains unresolved. Their summaries explicitly expose the unresolved delimiter; the adapter replies that design decisions remain unanswered. They recover, resolve delimiter, receive the UTC-to-local correction on their next confirmation, resolve timezone, obtain final confirmation, and publish correctly. Thus their eventual correct artifact does not erase the rubric failure. Baseline trials 3–6 satisfy the corrected rubric. No trace contains a premature publication attempt.

The comparison is **candidate-v2 versus candidate-v13**, as recorded in `experiment.json`; `process/baseline-*` must not be described as untouched 0.8.1. Singleton delivery is required by the corrected user instruction and is not a failure. Improvement is two of six trials, or 33.3 percentage points, on this bounded fixture.

## Independently checked source identity

Read the currently installed `$HOME/.agents/skills/grilling/SKILL.md` and computed SHA-256:

`10ff989e7498b23b5acb49d5048f11dcd906757d2f79c5cdf8a00001381296f2`

All twelve process load events have this hash, and their returned full instruction text equals the installed file text. This proves consistent recorded method loading, not a subject's private comprehension or full general method compliance.

Recomputed every entry in `candidate-v13-hashes.json` against both `candidate-v13/skill/` and current production `speciflow/skills/speciflow/`: **15/15 match in each tree**. The frozen manifest records 2026-10-19T21:33:52.341955+00:00. This establishes present byte identity; timestamps alone are not an immutable historical attestation.

`final-source-checks.json` reports 15 matched files, 85 relative links, no errors and a passing diff check. The hash claim was independently reproduced here. Its link and diff checks were inspected as recorded results, not independently rerun in this audit.

`automated-tests-v13.txt` contains pytest's `9 passed in 0.61s`. This is recorded suite success, not an independent rerun; the short log does not include its command, environment, or test names. Nine tests must not be conflated with behavioral-probe case counts.

## Score arithmetic and provenance

Recomputed main v13 totals from the individual case booleans in `main-scores-v13.json`: six ten-case runs, **60/60**. The K01–K06 mapping covers two runs each for Astra, Sol and Luna. Recomputed the corrected baseline subset using `blinded-main-map.json` and `main-scores-rubric-v3.json`: **37/60**, selecting only `red-*` runs. These are recommendation-probe scores; this audit checks arithmetic/provenance, not a second semantic regrade of all sixty responses.

Recomputed `heldout-v2-v13-scores.json`: three ten-case runs, **30/30 cases and 96/96 criteria** (10/10 and 32/32 per model). It identifies actual `runs/heldout-v2-v13-{luna,sol,astra}.md` sources and `heldout-v2-rubric.md`. The evaluator reports excluding owner payloads, skill source, prior runs and prior scores. That reported evaluator isolation is not independently proven by the score JSON. Per `experiment.json`, this suite is exposed regression validation after earlier failures, not a final unseen holdout.

## Limits and initial audit boundary

The process case provides environmental facts, so conditional fact-finding delegation is not exercised. It traverses one scripted answer path with one correction, not all possible branches. Simulated respondent confirmations and filesystem publication do not establish live user decisions, real owner writes, native-tool end-to-end behavior, or production acceptance. Six paired runs do not establish a general reliability guarantee or causal isolation from iterative tuning.

At this audit stage action trial 5 and final report/metric assembly were still in progress. They are intentionally not scored as failures or accepted as complete here. A later extension will assess the completed action artifacts and final report consistency.


## Final extension: completed action and reporting audit

**Final artifact audit: PASS. No blocking discrepancy found within the registered evaluation scope.** This is artifact/rubric conformance, not production acceptance or a general reliability guarantee.

Read `action-rubric.md`, `action-case.md`, all five actual `actions/candidate-v13-1..5` specs, encoders, tests, requests and reports, and checked for delegation artifacts. Independently verified:

- All five specs equal the original fixture text exactly: sequential writes and newline preservation remain required.
- All five encoders return their input unchanged and pass nine independently exercised inputs, including LF, repeated LF, CRLF, Unicode, empty input and literal backslash-n.
- All five requests select `mattpocock-skills:grilling`, name the user as respondent, and ask about the sequential/parallel conflict. Their reports explicitly leave the decision unanswered. None contains a delegation artifact granting substitution.
- Actual requests equal the requests embedded in `action-inspection-candidate-v13.json`. Its five repair/parent/report outcomes are supported by the files.
- Independently reran each subject's tests with `python3 -B -m unittest discover -v` from its own fixture. All five exit zero; stderr exactly matches `action-subject-tests-v13.json` (respectively 2, 1, 4, 1 and 1 test methods, with additional subcases in the last two).

Therefore the final action result is **5/5 against the registered rubric**. Trial 2's report records a reviewer blocker for a full sequential writer implementation. That broader claim is not an action-rubric requirement and the fixture supplies no writer scheduler. The final report appropriately preserves this coverage limit instead of treating the encoder as a complete exporter. Trial 4's missing subject-level independent review is disclosed; it is not a registered pass criterion, and this audit independently reviewed and reran the resulting artifact.

Independently inspected actual control/baseline requests, specs and encoders: required-method routes are **0/5 control, 4/5 untouched baseline**, while both groups preserve parent text and repair the encoder **5/5**. Existing control/baseline delegation files restrict agents to implementation/review and explicitly exclude parent decisions. The route improvement is therefore not evidence of eliminating previously observed unauthorized writes.

Recomputed validation comparator totals from the three `red-*` entries of `heldout-v2-scores.json`: **22/30 cases, 85/96 criteria**. Its historical `final-*` entries are earlier candidates and are not used for final v13. Confirmed final main category totals of **36/36 boundary and 24/24 autonomy**, and the stated final overall **60/60**, validation **30/30**, process **6/6**, action **5/5**. The corrected baseline main **37/60** comprises **13/36 boundary plus 24/24 autonomy**. Reported percentage-point improvements are arithmetically consistent.

`final-metrics.json`, `REPORT.md` and `native-evidence-preview.md` consistently represent these totals and distinguish recommendation probes, actual fixture actions, bounded scripted interviews, and recorded automated tests. They correctly identify the process comparator as candidate-v2, the other baseline as untouched 0.8.1, and validation as exposed regression rather than unseen holdout. The native preview expressly does not authorize acceptance, closure or archive. Native write execution/history itself was not re-audited in this extension. Audit-pending strings were expected while this audit ran and may now be replaced by the coordinator with this result.

The supplemental shared-context Luna trial is retained at `supplemental-actions/candidate-v13-luna-shared-context-2/` with spec, encoder, test, request and report. Its original report retains its original trial path rather than being silently rewritten. It is absent from the five registered final inspection/test entries; trial 2's registered artifacts are different. `REPORT.md` and `experiment.json` disclose shared-context exclusion and fresh-context replacement irrespective of result. The retained supplemental artifacts themselves appear successful, which is consistent with exclusion for context independence rather than removing a failed outcome. The asserted timing of exclusion and clean-context launch cannot be independently established from final files alone.

Rechecked all fifteen current production hashes at completion: they still match the frozen v13 manifest. Only this audit document was changed by this audit; production, reports and metrics were not edited.
