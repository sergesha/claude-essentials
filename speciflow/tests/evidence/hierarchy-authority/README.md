# Hierarchy and original-grilling release evidence

This portable extract accompanies the hierarchy/authority correction after Speciflow 0.8.1. It records the completed candidate-v13 campaign, not a new experiment. Home-directory prefixes in exported evidence are replaced with `$HOME`; original local evidence remains unchanged.

| Measure | Comparator | Candidate-v13 |
| --- | ---: | ---: |
| Main recommended actions | 37/60 | 60/60 |
| Boundary cases | 13/36 | 36/36 |
| Autonomy controls | 24/24 | 24/24 |
| Additional regression cases | 22/30 | 30/30 |
| Additional criteria | 85/96 | 96/96 |
| Original-method interview | 4/6 | 6/6 |
| Actual fixture: required-method request | 4/5 | 5/5 |
| Actual fixture: unchanged parent / encoder repair | 5/5 / 5/5 | 5/5 / 5/5 |

The main gain is 38.3 percentage points. Main probes use two fresh subjects each for gpt-6-luna medium, gpt-6.1-sol medium and gpt-6-astra high; validation uses one per model. Six actual interviews use two per model. Five actual action fixtures use Luna twice, Sol twice and Astra once. Model and effort vary together. Existing automated tests pass 9/9; they are separate from behavioral scores.

## Inspect the evidence

- [Main cases](cases.md), [final rubric](rubric-v3.md), [corrected historical scores](main-scores-rubric-v3.json), [final scores with answer evidence](main-scores-v13.json).
- [Additional cases](heldout-v2-cases.md), [rubric](heldout-v2-rubric.md), [historical scores](heldout-v2-scores.json), [final scores](heldout-v2-v13-scores.json). Only `red-*` historical runs form the 22/30 baseline; historical `final-*` runs are intermediate candidates.
- [Action case](action-case.md), [rubric](action-rubric.md), [actual artifact inspection](action-inspection-candidate-v13.json), [subject test reruns](action-subject-tests-v13.json).
- [Interview case](process-case.md), [singleton rubric](process-rubric-v2.md), [comparator inspection](process-baseline-singleton-inspection.json), [final inspection with plan contents](process-candidate-v13-singleton-inspection.json).
- [Independent artifact audit](artifact-audit-v13.md), [frozen skill hashes](candidate-v13-hashes.json). The hash manifest covers skill files, not release version manifests.

## Corrections, failures and limits

The main/action comparator is untouched 0.8.1. The interview comparator is candidate-v2 before the full-process clarification, **not** untouched 0.8.1. Both failed interview trials requested confirmation with delimiter unresolved, then recovered; none published before confirmation. One-question-at-a-time delivery is correct. The initial batching criterion was withdrawn following explicit user clarification.

Independent blinded validity reviews corrected two overconstraints: rejection of an unsupported addition need not first investigate it, and rejection of an unnecessary addition need not trigger an interview. Necessary live parent amendments still require original user grilling. Uniform correction moved the main baseline from 35/60 to 37/60. Conservative case-4 regrading reduced intermediate v1/v2/v5 to 59/60; earlier all-pass statements are superseded.

Validation case H02 was clarified to include its required independent-repair grant, then baseline and candidates used the same revision. Validation is exposed regression after iterative fixes, **not an unseen holdout**. v5–v9 validation each scored 29/30; v10 scored 28/30; v11/v12 scored 30/30 but main scored 58/60 and 59/60. These failures motivated general routing, owner-order, retained-decision and authorized-work continuation clarifications. Final frozen v13 passed the complete registered matrix.

A successful second Luna action trial accidentally shared context with its first trial. It was excluded for context independence before final scoring and replaced with a fresh-context trial irrespective of outcome; the supplemental successful artifact remains in the local archive. Known-failing candidates did not run every unstarted trial. No completed failed trial was silently discarded.

Recommended-action scores are not actual owner mutations. Action fixtures repair an encoder and preserve the parent, recording an unanswered method request; they neither execute the interview nor implement a writer scheduler. All original action baselines already preserved the parent and repaired the defect. Interview fixtures execute the loaded original method through a scripted respondent transport, including a correction that opens a dependent question; environmental facts are supplied, so conditional fact-finding delegation is untested. Selected small samples and repeated tuning do not establish a production reliability rate or universal compliance.

Full raw traces, every intermediate candidate, original and superseded rubrics, native-owner logs and the transport adapter remain in the local `research/speciflow-contract-tests/` archive; this extract does not claim to contain that full archive or independently prove evaluator isolation. The audit distinguishes directly reproduced observations from recorded evidence. Native task identifiers are not required to review the released skill.
