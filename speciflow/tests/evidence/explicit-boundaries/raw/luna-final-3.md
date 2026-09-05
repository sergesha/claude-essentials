A — Partial project

Immediate action: дать краткий read-only `status` с четырьмя раздельными строками; ничего не устанавливать и не изменять.

Ответ пользователю: «Краткий статус на 2026-09-05 12:00 UTC: OpenSpec — valid, root `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, status и validation успешны (CLI 1.11.0). Superpowers — valid, closure читаем. Backlog.md — N/A (корня проекта нет). Beads/Dolt — N/A (исполняемая работа не назначена). Изменений нет.»

B — Approved concrete writes

Immediate action: выполнить штатные native-create команды Beads от основного агента в `/tmp/atlas-beads` для ровно P1 и P2 с показанными payload, source references и commit effects.

Ответ пользователю: «Review: required — completed, no blockers. Выполняю ровно две подтверждённые записи P1/P2 штатными командами Beads от основного агента; дополнительные вопросы не нужны.»

C — Storage boundary

Immediate action: оставить `.speciflow-project.json` без изменений и не переносить в него этап, задачи или контекст.

Ответ пользователю: «Это единственный служебный metadata-файл SpeciFlow: он нужен только для проверки коллизии project identity по пути хранения. Он не хранит этап, задачи или состояние workflow; native planning и Beads остаются в своих корнях. Ничего переносить и менять не нужно. Завтра контекст придётся передать обычным сообщением/заметкой владельца.»

D — Analysis-only approval

Immediate action: показать точный preview двух кандидатов Beads (payload, stable source references, root и commit effects) и запросить отдельное явное одобрение записей; issues не создавать.

Ответ пользователю: «Анализ согласован, но создание записей ещё нет. Сначала покажу точный preview двух Beads issues и попрошу подтвердить именно эти native writes; “продолжай” само по себе их не разрешает.»

E — Ordinary short approval

Immediate action: выполнить штатные native-create команды Beads от основного агента в `/tmp/atlas-beads` для ровно P1 и P2 по непосредственно показанному preview.

Ответ пользователю: «Review: required — completed, no blockers. Да, подтверждение покрывает показанный конкретный preview: создаю P1 и P2 штатными командами Beads от основного агента.»
