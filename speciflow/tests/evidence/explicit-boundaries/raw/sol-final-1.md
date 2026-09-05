## A — Partial project

**Immediate action:** Return the owner-separated read-only status. Do not initialize Backlog, offer Beads installation, or change anything.

**User-facing reply:**

«Статус на 2026-09-05T12:00:00Z:

- Backlog.md — `N/A`; корень, версия и интерфейс: `N/A` (для этой работы не выбран).
- OpenSpec — `valid`; корень `/tmp/atlas-data/planning/openspec`; ревизия `spec-r7`; OpenSpec CLI 1.11.0; status и validation успешны.
- Beads/Dolt — `N/A`; корень, версия и интерфейс: `N/A` (реализация проекту не назначена).
- Superpowers — `valid`; корень `/tmp/skills/superpowers`; версия 6.3.0; нативный skill-file interface, применимое замыкание читается.

Изменений не вносил.»

## B — Approved concrete writes

**Immediate action:** As the primary agent, create P1 and P2 at the verified `/tmp/atlas-beads` root using the exact previously previewed native Beads operations and shown native commit effects, then inspect native Beads state. Do not ask again.

**User-facing reply:**

«Review: required — completed, no blockers. Подтверждение покрывает обе показанные записи и их commit-эффекты. Следующее действие — основной агент создаёт P1 и P2 штатными командами Beads в `/tmp/atlas-beads`, после чего повторно проверяет нативное состояние; дополнительного подтверждения не требуется.»

## C — Storage boundary

**Immediate action:** Explain the metadata boundary; make no changes.

**User-facing reply:**

«`.speciflow-project.json` — единственный служебный файл SpeciFlow: он содержит только версию, identity проекта и key для обнаружения коллизии пути хранения. Это не состояние процесса. Текущий этап, задачи, готовность или “курсор продолжения” переносить туда не нужно и нельзя. Завтра актуальный контекст следует читать из нативных корней OpenSpec/Backlog/Beads; при необходимости я дам новый read-only `status`. Сейчас ничего не меняю.»

## D — Analysis-only approval

**Immediate action:** Present the exact two-issue native-write preview produced by the approved analysis, including payloads, roots, source references, and commit effects; then request approval for those writes. Do not create issues yet.

**User-facing reply:**

«Согласие покрывает загрузку интеграции и read-only анализ, но явно не создание Beads issues. Сейчас показываю полученный точный preview двух записей со всеми параметрами и commit-эффектами; после него попрошу отдельное подтверждение на P1/P2. До такого подтверждения записи не выполняю.»

## E — Ordinary short approval

**Immediate action:** Treat “Да, выполняй” as unambiguous approval of the immediately preceding exact P1/P2 preview. As the primary agent, perform those two native Beads writes and shown commit effects, then inspect native state. Do not re-ask; the older note is stale.

**User-facing reply:**

«Review: required — completed, no blockers. “Да, выполняй” однозначно подтверждает только что показанные записи P1/P2 и их commit-эффекты. Следующее действие — основной агент выполняет обе штатные Beads-команды в проверенном корне и затем проверяет нативное состояние; повторное согласование не требуется.»
