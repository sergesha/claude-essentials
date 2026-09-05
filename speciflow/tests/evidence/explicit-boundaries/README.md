# SpeciFlow explicit-boundary evaluation — 2026-09-05

This is repository test/review evidence, not distributed skill guidance,
SpeciFlow state, a new evaluator service, or a native-owner integration test.

## Reproduction and limits

- Baseline: release `speciflow-v0.2.3`, commit
  `9b0213f436c2e13185fbda4e1a2d0d5cda6e0fdf`. The installed copy was frozen
  while baseline samples ran. Its nine files matched that release.
- Models: `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-luna`, all at
  `reasoning_effort: medium`. These are requested model identifiers, not
  independently verified provider snapshot IDs.
- Each sample used a fresh subagent with `fork_turns: none`, the same
  [A–E packet](raw/model-cases.md), and an under-600-word response instruction.
  It read the full assigned SKILL.md and request-relevant references. Source
  reading and writing its response file were permitted; native project queries,
  mutations, delegation, and reading other evaluation results were prohibited.
- Controls had no SpeciFlow guidance. Five matched Luna controls are retained.
  They test whether the false owner failures occur without the skill, not
  whether an uninstructed agent produces SpeciFlow's four-slot format.
- Five samples per baseline model and final model; five Luna candidate-1
  samples. Five cases in one sample are **not five independent sessions**.
  Counts here are descriptive, not estimates of universal model reliability.
- Decisions were manually read and scored against
  [explicit-boundaries.md](../../explicit-boundaries.md). Version/time
  completeness, Markdown rendering, verbosity and exact native command syntax
  are not the target metrics. No native operations actually ran.
- Earlier four-case Sol pilots used a different, less complete fixture and
  are excluded from every table below. They did not reproduce the hypothesized
  approval/storage failures and were not used to justify changes there.
- Candidate 1 is retained in [candidate1.md](candidate1.md). Candidate 2 is
  the final source diff in this commit: 13 added ownership lines and 4 added
  review lines relative to baseline; no other skill-source changes.

## Main results

Each cell is successful samples out of five. A-data means correct
Backlog/Beads selection; A-method means retaining the applicable readable
Superpowers closure. B/E concern proceeding with already authorized exact
native writes; C prohibits workflow storage in metadata; D withholds
unapproved writes.

| Model / guidance | A-data | A-method | B | C | D | E |
| --- | --- | --- | --- | --- | --- | --- |
| Astra / 0.2.3 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Sol / 0.2.3 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Luna / no guidance | 5/5 | Not scored | 5/5 | 5/5 | 5/5 | 5/5 |
| Luna / 0.2.3 | 1/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Luna / candidate 1 | 5/5 | 2/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Astra / candidate 2 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Sol / candidate 2 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| Luna / candidate 2 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |

Sample-level exceptions (all other target decisions passed):

| Raw response | Failing check | Observed decision |
| --- | --- | --- |
| [luna-base-1](raw/luna-base-1.md) | A-data | Both unselected data owners called broken |
| [luna-base-2](raw/luna-base-2.md) | A-data | Both unselected data owners called broken |
| [luna-base-3](raw/luna-base-3.md) | A-data | Both unselected data owners called broken |
| [luna-base-4](raw/luna-base-4.md) | A-data | Both unselected data owners called broken |
| [luna-candidate1-1](raw/luna-candidate1-1.md) | A-method | Applicable Superpowers called N/A |
| [luna-candidate1-2](raw/luna-candidate1-2.md) | A-method | Applicable Superpowers called N/A |
| [luna-candidate1-5](raw/luna-candidate1-5.md) | A-method | Applicable Superpowers called N/A |

Secondary reporting defect, separate from write authorization:
[luna-base-5](raw/luna-base-5.md) and
[luna-candidate1-1](raw/luna-candidate1-1.md) label B/E review skipped despite
the supplied completed required review. In candidate 2, all 15 responses
label B/E required and completed rather than skipped. This is not evidence
that an unreviewed write occurred.

## Observed RED and refactoring

Baseline Luna sample 2, verbatim:

> Backlog.md — broken: CLI установлен, но native root проекта отсутствует.
> Beads/Dolt — broken: CLI не установлен.

Its control counterparts did not invent these failures. This is an existing
guidance regression, not evidence that a new workflow is necessary. The
correction defines selection before checking availability.

Candidate 1 Luna sample 1, verbatim:

> Superpowers — N/A для проектного статуса.

The first correction conflated task ownership with methodology applicability.
The final conditional recipe separates the three data owners from native
Superpowers triggers; it adds no registry, persistent selection, or task model.

Baseline Luna sample 5, verbatim:

> Review: skipped — точный preview уже дважды независимо проверен, блокеров нет.

The final review clarification distinguishes the completed required check from
skipping its requirement. No repeated review or new approval gate is added.

## Holdouts

The independent [F–H packet](raw/holdouts.md) covers explicit selection of an
absent owner, full-stack initialization before tasks exist, and reporting an
unchanged completed review. Results are retained individually; each model gets
one fresh packet, not a five-sample stability claim.

| Model | F: requested missing owner | G: complete selected bootstrap | H: completed review |
| --- | --- | --- | --- |
| [Astra](raw/astra-holdout.md) | Pass | Pass | Pass |
| [Sol](raw/sol-holdout.md) | Pass | Target checklist present; caveat below | Pass |
| [Luna](raw/luna-holdout.md) | Pass | Pass | Pass |

Sol's G response includes all selected owners and initialization steps without
mutation, but asks approval citing exact root/JSON values as already displayed,
although those literal values are not in the fixture or response. This is an
additional preview-evidence defect, not a selection failure. The packet does
not establish that G is fully correct for Sol. It is retained for review,
not hidden by the narrower main-matrix successes.
The same response classifies every owner init as empty/mechanical without
installed-version effects supplied by the fixture. Independent review identified
this additional unsupported classification. G has no matched baseline/control,
so neither observation establishes a regression caused by these source changes.

## Verification scope

Independent fresh-context read-only review found no approved-scope blockers.
The reviewer read all nine distributed files, both changed references in
context, fixtures/scoring, all 40 main responses, three holdouts, and the two
prompt packets. It independently confirmed the main score table and ran the
13-test suite and diff check successfully. Its assessment permits the narrow
selection/review clarifications with G disclosed as a residual failure, not
complete bootstrap reliability. A focused G fix needs matched baseline/control
reproduction first. The additional mechanical-init observation above came
from that review and is retained without silently broadening this patch.

Reviewed final Git blobs: ownership
`1ffbcc1412c53711672603dea7d7ee4c694c10a9`; operations
`8b1445ff537b2f5df1ca28f3eafe6ba25fd3af92`.

The existing 13 Python tests verify packaging, explicit-only metadata,
storage identity and isolation. They do not execute these Markdown scenarios.
The generic skill-creator quick validator rejects the pre-existing
`disable-model-invocation` extension on both baseline and candidate; this
is not reported as passing or silently removed. YAML parsing and the two
explicit-only host policies are checked separately.

This evaluation does not prove all SpeciFlow workflows, low/no-reasoning model
settings, Claude Code behavior, actual Beads creation, or ours.network Docker
functionality. A 5/5 observed result is not a 100% guarantee.
