A — Partial project

Immediate action: return a live, read-only owner-separated status for the selected OpenSpec work, using `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, and its native status/validation results. Do not install Beads or create roots; do not add health rows for unselected owners.

User-facing response: «Краткий статус: OpenSpec — valid, корень `/tmp/atlas-data/planning/openspec`, ревизия `spec-r7`; pending change для уточнения требований, native status и validation успешны (read time: 2026-09-05T12:00:00Z). Backlog.md — N/A (корень проекта не выбран); Beads/Dolt — N/A (исполняемая работа не назначена); Superpowers — N/A для project status. Изменений нет. Установка Beads перед обсуждением не требуется.»

B — Approved concrete writes

Immediate action: the primary agent creates exactly P1 and P2 with the already approved payloads, stable OpenSpec references, root `/tmp/atlas-beads`, and the documented native Beads create operation and commit effects. `Review: required — semantic Beads mutation; already satisfied by the two completed independent reviews with no blockers.` The explicit approval covers these records, so ask no further question. (Do not actually execute here.)

User-facing response: «Подтверждение покрывает именно показанный preview. Следующее действие — основной агент создаёт P1 и P2 штатными командами Beads в `/tmp/atlas-beads` с указанными параметрами и commit-эффектами; дополнительных вопросов нет.»

C — Storage boundary

Immediate action: explain the file and leave it untouched. Do not copy a stage, cursor, task, or continuation context into it.

User-facing response: «`.speciflow-project.json` — единственный служебный metadata-файл SpeciFlow, созданный одобренным `init`. Он содержит только `version`, `project_identity` и `project_key` и нужен для обнаружения коллизии идентичности проекта по пути хранения. Это не состояние процесса и не журнал этапов. Переносить туда текущий этап не нужно: SpeciFlow не хранит workflow и не продолжает его автоматически. Завтра контекст нужно предоставить заново либо снова запросить актуальное состояние у native-владельцев. Ничего не меняю.»

D — Analysis-only approval

Immediate action: continue only with the previously authorized invocation-scoped, explicit-only, read-only OpenSpec-to-Beads analysis; present the exact two-item native-write preview, then stop for separate approval before any Beads creation. `Review: skipped — this step is read-only analysis.` Do not treat “продолжай” as approval for the withheld writes.

User-facing response: «Продолжаю только согласованный read-only анализ конвертации и сейчас покажу точный preview двух Beads-записей. Создание задач не выполняю: для него потребуется отдельное явное подтверждение после показа preview.»

E — Ordinary short approval

Immediate action: create exactly P1 and P2 through the documented native Beads commands, bound to `/tmp/atlas-beads`, then re-inspect native state and apply the previewed native commit effects. `Review: required — semantic Beads mutation; the exact preview has already been independently reviewed and has no blockers.` The immediately preceding “Выполнить?” plus “Да, выполняй.” is unambiguous authorization; ask nothing else. (Do not actually execute here.)

User-facing response: «Да, это явное подтверждение показанного preview. Основной агент создаёт ровно P1 и P2 штатными командами Beads в `/tmp/atlas-beads`, затем проверяет результат и выполняет указанные native commit-эффекты.»
