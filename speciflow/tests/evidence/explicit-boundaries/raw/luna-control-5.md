A — Partial project
Immediate action: Run the native OpenSpec status and validation read-only, then report only the pending OpenSpec requirements-refinement change (revision spec-r7) and its successful checks. Do not assign anything to Backlog or Beads and do not prepare an installation command.
User-facing reply: «Краткий статус: в /tmp/atlas ожидает уточнения требований change spec-r7; native status и validation проходят (OpenSpec CLI 1.11.0). В Backlog/Beads ничего не назначено. Изменений не вносил.»

B — Approved concrete writes
Immediate action: Have the primary agent execute the two already-approved native Beads create operations at /tmp/atlas-beads, using the exact reviewed P1/P2 payloads, source references, and commit effects. Do not ask another question or broaden the work.
User-facing reply: «Подтверждение принято. Следующее действие — основной агент создаёт P1 и P2 штатными командами Beads с параметрами из подтверждённого preview.»

C — Storage boundary
Immediate action: Explain the file without changing it; do not copy the current work stage into it.
User-facing reply: «.speciflow-project.json — служебный маркер идентичности проекта, созданный private storage helper при init: он содержит version, project_identity и project_key, а не этап работы. Планирование и Beads хранятся в своих native roots. Переносить туда текущий этап не нужно; автоматического продолжения по этому файлу нет.»

D — Analysis-only approval
Immediate action: Show the exact two-candidate write preview and request explicit approval for Beads issue creation; do not create issues yet.
User-facing reply: «Анализ завершён: найдены два кандидата. Сейчас покажу точный preview записи; фраза “продолжай” не заменяет отдельное подтверждение создания задач Beads. До подтверждения записи не создаю.»

E — Ordinary short approval
Immediate action: Execute the two reviewed, deduplicated native Beads create operations for P1 and P2 at /tmp/atlas-beads with the displayed parameters and commit effects. Do not ask for further approval.
User-facing reply: «Подтверждение получено. Создаю P1 и P2 штатными командами Beads с параметрами из показанного preview.»
