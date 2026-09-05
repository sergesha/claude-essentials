A — Частичный проект

Немедленное действие: выполнить live-read-only `status` через OpenSpec и показать четыре раздельные строки на время чтения 2026-09-05T12:00:00Z. OpenSpec — `valid` (корень `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, CLI 1.11.0); Backlog.md — `N/A` (не выбран, native root отсутствует); Beads/Dolt — `N/A` (не выбран, CLI не установлен); Superpowers — `valid` (closure читаем). Ничего не устанавливать и не изменять.

Короткий ответ пользователю: «Краткий статус на 12:00Z: OpenSpec valid — pending change `spec-r7`, проверка успешна; Backlog и Beads не выбраны для этой работы (N/A); Superpowers closure доступен. Изменений нет. Подготовка установки Beads не требуется для этого статуса.»

B — Подтверждённые конкретные записи

Немедленное действие: создать ровно P1 и P2 штатными командами Beads в `/tmp/atlas-beads` с параметрами из показанного preview, стабильными ссылками на revision и предусмотренными native commit effects. `Review: required — semantic Beads mutation; exact preview independently reviewed twice with no blockers.` Подтверждение пользователя покрывает сами две записи, поэтому дополнительное согласие не запрашивается. Команды не выполнять в этом сравнении.

Короткий ответ пользователю: «Принято. Выполняю ровно две подтверждённые записи P1/P2 через native Beads в `/tmp/atlas-beads`; после выполнения перепроверю native state и сообщу IDs и commit-результат.»

C — Граница хранения

Немедленное действие: ничего не менять и объяснить назначение файла. `.speciflow-project.json` — единственная метаинформация SpeciFlow, созданная helper’ом при init; это ровно `version`, `project_identity` и `project_key`, используемые для обнаружения коллизии идентичности по пути `<base>/projects/<project-key>/.speciflow-project.json`. Он не хранит этап, очередь, статус, approval или состояние native owners.

Короткий ответ пользователю: «Переносить туда текущий этап не нужно и нельзя: SpeciFlow не хранит workflow-состояние. Текущие данные остаются в native roots OpenSpec и Beads; завтра контекст нужно получить новым live status/из диалога.»

D — Одобрена только аналитика

Немедленное действие: показать точный item-by-item preview двух кандидатов Beads с payload, source reference, root, native create operation и commit effects; не создавать записи. `Review: skipped — read-only analysis.` После preview запросить отдельное, недвусмысленное подтверждение именно этих native writes: «Выполнить?» Слово «продолжай» не расширяет прежнее разрешение на analysis-only.

Короткий ответ пользователю: «Продолжаю только согласованную read-only конвертацию: сейчас покажу точный preview двух Beads-записей. Создание не выполняю без отдельного подтверждения этого preview.»

E — Обычное короткое подтверждение

Немедленное действие: выполнить штатные native Beads create для ровно P1 и P2 в `/tmp/atlas-beads` с показанными параметрами и commit effects. `Review: required — semantic Beads mutation; exact preview reviewed and deduplicated, no blockers.` Сообщение «Да, выполняй» однозначно покрывает непосредственно предшествующий конкретный preview; дополнительный вопрос не нужен. После выполнения перепроверить native state и сообщить IDs/commit result. Команды не выполнять в этом сравнении.

Короткий ответ пользователю: «Принято. Выполняю ровно P1 и P2 штатными командами Beads в `/tmp/atlas-beads`; затем проверю native state и сообщу результат.»
