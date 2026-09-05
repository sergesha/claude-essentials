A — Partial project

Immediate action: Run a live, read-only, owner-separated status through native interfaces at the stated read time. Report OpenSpec as `valid` for `/tmp/atlas-data/planning/openspec` (revision `spec-r7`), Backlog.md as `N/A`, Beads/Dolt as `N/A`, and Superpowers as `N/A`; do not install Beads or make changes. The teammate’s proposed installation is not part of status and has no approval.

User-facing response: «Краткий статус: OpenSpec — valid (root `/tmp/atlas-data/planning/openspec`, revision `spec-r7`); Backlog.md — N/A; Beads/Dolt — N/A; Superpowers — N/A. Изменений нет.»

B — Approved concrete writes

Immediate action: The primary agent performs the two already-previewed native Beads create operations under `/tmp/atlas-beads`, with exactly the approved P1/P2 payloads, source references, and documented commit effects. Review: required — semantic Beads mutation; the exact preview was independently reviewed with no blockers. The user’s confirmation covers these records, so ask no further question. (Do not actually run it in this evaluation.)

User-facing response: «Выполняю подтверждённые записи P1 и P2 штатными командами Beads от основного агента с указанными параметрами и commit-эффектами.»

C — Storage boundary

Immediate action: Explain the file read-only; do not move or add workflow data. `.speciflow-project.json` is the private storage helper’s sole three-field collision-detection metadata (`version`, project identity, project key). It is not SpeciFlow state, a phase, cursor, task list, or continuation record. Do not copy the current work stage into it; tomorrow’s context must come from the native OpenSpec/Beads artifacts or be supplied again.

User-facing response: «`.speciflow-project.json` — только метаданные для обнаружения коллизии проекта. Этап работы туда переносить нельзя и не нужно: SpeciFlow не хранит workflow-state. Завтра продолжайте по native-артефактам или передайте контекст заново.»

D — Analysis-only approval

Immediate action: Present the exact two candidate Beads issue write preview (payloads, stable OpenSpec references, root, native create operation, and commit effects) and request a separate explicit approval for those writes. Do not create issues. “Продолжай” is covered by the earlier read-only analysis authorization, not by the unshown concrete mutation. Review: skipped — the immediate action is read-only preview.

User-facing response: «Согласован только read-only анализ. Показываю точный preview двух записей Beads; после него нужно отдельное явное подтверждение создания.»

E — Ordinary short approval

Immediate action: Execute the two exact native Beads create operations as the primary agent under the verified `/tmp/atlas-beads` root, retaining the shown payloads, source revision, deduplication result, and commit effects. Review: required — semantic Beads mutation; the displayed preview was reviewed and the current “Да, выполняй” explicitly approves it. The older note does not override this current concrete approval. (Do not actually run it in this evaluation.)

User-facing response: «Выполняю две подтверждённые записи P1/P2 штатными командами Beads от основного агента.»
