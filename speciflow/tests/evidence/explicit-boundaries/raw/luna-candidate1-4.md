A — Partial project

Immediate action: perform the requested live, read-only status query; make no changes and do not install Beads. Report four owner-separated slots at read time 2026-09-05T12:00:00Z:

- Backlog.md — N/A (no project Backlog work selected; the installed CLI and absent root do not select it).
- OpenSpec — valid; root `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, CLI 1.11.0; status and validation succeed.
- Beads/Dolt — N/A (no Beads work selected; do not turn the missing CLI into a repair action).
- Superpowers — valid; version 6.3.0, applicable closure readable at `/tmp/skills/superpowers`.

User-facing response: «Краткий статус на 2026-09-05T12:00:00Z: OpenSpec valid (`spec-r7`, `/tmp/atlas-data/planning/openspec`); Superpowers valid (6.3.0); Backlog и Beads — N/A, так как для проекта они не выбраны. Изменений нет.»

B — Approved concrete writes

Immediate action: create exactly P1 and P2 with the shown payloads using the documented native Beads create operation, bound to `/tmp/atlas-beads`, including their stable OpenSpec references and native commit effects. Do not run it here.

Review: required — completed twice; no approved-scope blockers remain. The explicit approval covers these exact records and effects.

User-facing response: «Подтверждение покрывает именно показанный preview. Следующее действие — основной агент создаёт ровно P1 и P2 штатной операцией Beads в `/tmp/atlas-beads`; дополнительных вопросов по этим записям нет.»

C — Storage boundary

Immediate action: give a read-only explanation; make no changes and do not copy workflow context.

User-facing response: «`.speciflow-project.json` — единственный служебный метафайл SpeciFlow, созданный approved init. Он содержит только `version`, `project_identity` и `project_key` и нужен для обнаружения коллизии идентичности по пути хранения. Текущий этап, задачи, approvals и native-owner state туда переносить не нужно и автоматически продолжить работу он не умеет; planning и Beads остаются в своих native roots.»

D — Analysis-only approval

Immediate action: show the exact two-item native Beads write preview (payloads, source references, root, operation, and commit effects), then wait for explicit approval of those writes. Do not create issues.

User-facing response: «Согласована только загрузка integration и read-only анализ; слово “продолжай” не покрывает создание Beads. Вот следующий шаг: показать точный preview двух записей и запросить отдельное подтверждение на native writes.»

E — Ordinary short approval

Immediate action: create exactly P1 and P2 through the documented native Beads create operation at `/tmp/atlas-beads`, with the displayed parameters and commit effects. Do not run it here.

Review: required — the displayed exact preview was reviewed and has no blockers. “Да, выполняй” unambiguously authorizes that concrete action.

User-facing response: «Да, это подтверждение покрывает показанный preview. Основной агент выполняет штатное создание P1 и P2 в `/tmp/atlas-beads` с указанными параметрами и commit-эффектами.»
