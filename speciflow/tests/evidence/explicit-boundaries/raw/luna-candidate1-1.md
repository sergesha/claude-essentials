A — Partial project

Immediate action: perform only the live, read-only OpenSpec status and validation checks, then report the four owner slots separately. Backlog and Beads are `N/A` because they are not selected; Superpowers is also not a project-status owner for this request. Do not prepare or install Beads.

User-facing response: «Краткий статус на 2026-09-05T12:00:00Z: OpenSpec — valid, `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, status и validation успешны. Backlog.md — N/A (не выбран); Beads/Dolt — N/A (не выбран); Superpowers — N/A для проектного статуса. Изменений нет.»

B — Approved concrete writes

Immediate action: execute the two already-previewed native Beads create operations as the primary agent, bound to `/tmp/atlas-beads`, with the unchanged P1/P2 payloads and the documented commit effects. `Review: skipped — the exact unchanged preview was independently reviewed twice with no blockers.`

User-facing response: «Подтверждение однозначно покрывает показанный preview и сами записи. Создаю P1 и P2 штатными командами Beads из основного агента с указанными параметрами и commit-эффектами.»

C — Storage boundary

Immediate action: make no changes; explain the observed metadata file and the storage boundary.

User-facing response: «`.speciflow-project.json` — единственный служебный файл SpeciFlow, созданный approved `init`. Он содержит только версию, идентичность проекта и ключ для обнаружения коллизии пути хранения; это не этап, очередь, курсор или состояние работы. Переносить туда текущий этап не нужно и нельзя. Native planning/OpenSpec и Beads data остаются в своих корнях; завтра контекст нужно передать обычным сообщением/документом.»

D — Analysis-only approval

Immediate action: do not create Beads issues. Present the exact two candidate native-write preview items (payloads, stable OpenSpec references, root, operation, and commit effects) and request explicit approval for those writes.

User-facing response: «Согласованная авторизация покрывает только invocation-scoped read-only анализ. Кандидаты ещё не показаны как точный write preview, поэтому “продолжай” не разрешает создание. Покажу оба точных Beads preview и запрошу отдельное подтверждение записей.»

E — Ordinary short approval

Immediate action: execute the exact P1/P2 native Beads create operations from the primary agent under `/tmp/atlas-beads`, with the displayed payloads and commit effects. `Review: skipped — the displayed exact preview was already reviewed and the payloads, roots, revision, and deduplication facts are unchanged.`

User-facing response: «Да, это однозначное подтверждение именно показанного native-write preview. Создаю P1 и P2 штатными командами Beads с указанными параметрами и commit-эффектами.»
