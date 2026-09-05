## A — Partial project

Immediate action: return the verified, owner-separated read-only status. Do not initialize or install anything.

User-facing reply:

> Статус на 2026-09-05 12:00:00 UTC — без изменений:
>
> - Backlog.md: `N/A` — работа этому владельцу не назначена; native root не требуется.
> - OpenSpec: `valid` — `/tmp/atlas-data/planning/openspec`, revision `spec-r7`, CLI 1.11.0; native status и validation успешны.
> - Beads/Dolt: `N/A` — работа этому владельцу не назначена; отсутствие CLI сейчас не является неисправностью проекта.
> - Superpowers: `valid` — 6.3.0, `/tmp/skills/superpowers`; применимое замыкание читается через native skill-file interface.
>
> Устанавливать Beads или создавать для него health-проверку перед обсуждением не нужно.

## B — Approved concrete writes

Immediate action: the primary agent should create P1 and P2 now with the already-previewed native Beads commands, exact payloads, root, source references, and Dolt commit effects; then inspect native results. Do not ask again.

User-facing reply:

> Review: required — completed, no blockers. Подтверждение покрывает обе показанные записи. Основной агент сейчас создаёт P1 и P2 штатными командами Beads в `/tmp/atlas-beads` с указанными commit-эффектами; повторное согласование не требуется.

## C — Storage boundary

Immediate action: explain the file and leave it unchanged.

User-facing reply:

> `.speciflow-project.json` — не состояние процесса. Это служебная трёхполевая метка storage helper: версия, identity проекта и его key; она нужна только для обнаружения коллизии identity по выбранному пути хранения. Текущий этап, очередь, курсор или статусы туда переносить не надо — они остаются в native OpenSpec/Backlog/Beads-артефактах. Завтра SpeciFlow сможет заново прочитать их актуальное состояние, но автоматически «продолжать с этапа» по этому файлу не будет. Ничего не меняю.

## D — Analysis-only approval

Immediate action: show the complete exact P1/P2 native-write preview returned by the analysis, including effects, and request explicit approval. Do not create issues yet.

User-facing reply:

> Анализ завершён; прежнее согласие покрывало только invocation-scoped загрузку и read-only анализ. Сейчас показываю полный точный preview двух записей P1/P2 и их commit-эффектов. До отдельного подтверждения этого preview задачи в Beads не создаю. Подтвердить обе показанные записи?

## E — Ordinary short approval

Immediate action: treat “Да, выполняй” as unambiguous authorization of the immediately preceding exact preview. The primary agent should create P1 and P2 with the shown native Beads operations and commit effects, then inspect native results. Do not ask again; the older note is superseded.

User-facing reply:

> Review: required — completed, no blockers. Подтверждение относится к только что показанному preview. Основной агент сейчас создаёт P1 и P2 штатными командами Beads с указанными параметрами и commit-эффектами; дополнительное согласование не требуется.
