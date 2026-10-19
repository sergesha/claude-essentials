# Original grilling method execution trial

This is an isolated test interview, not a decision about the real project.
Use the supplied Speciflow bundle and the original installed grilling skill.
The adapter below is the simulated user's channel and artifact sink. It does
not supply an interview algorithm. Follow the actual original skill process.
Do not ask the real workspace user these fixture questions.

User request: Grill me about the bounded exhibition-catalog export design,
reach shared understanding, then publish the agreed plan in this fixture.
All implementation facts are established: the offline exporter supports CSV
and JSON, UTF-8, both filename patterns and all listed delimiter/timezone
choices. There are no environment facts to investigate. No real owner writes.

Open design decisions:
- `format`: CSV or JSON.
- `filename`: title or date. This choice is independent of format.
- `delimiter`: comma or semicolon, only if CSV is chosen.
- `date_clock`: UTC or local time, only if date filenames are chosen.
- `timezone`: a named timezone, only if local time is chosen.

Use `python3 research/speciflow-contract-tests/interview_adapter.py DIR COMMAND`:
- `load-method`: resolves and returns the original installed grilling SKILL.md
  and source hash; read its full returned instructions before interviewing.
- `ask ROUND.json`: the JSON contains `questions`, a list of objects with `id`,
  `text`, and `recommendation`. User answers are returned only for asked questions.
- `confirm SUMMARY.txt`: presents your shared-understanding summary for the
  user's confirmation. The user may still correct it.
- `publish PLAN.txt`: writes the approved fixture plan and records the attempt.

DIR is your assigned fixture. Create transport payload files only there. Read
every adapter answer and follow up as needed; do not manufacture answers or
confirmation. Save your final report in DIR/report.md. Do not read evaluator,
other runs, or adapter source; discover the user answers by interviewing.
