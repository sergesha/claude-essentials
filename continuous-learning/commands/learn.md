---
description: Promote accumulated work findings into this project (skills/docs/commands)
argument-hint: "[--dry-run]"
---

Run the continuous-learning promotion pass for $ARGUMENTS (use the `continuous-learning` skill).

1. Load open findings (`mem_list`/`mem_search` tag `continuous-learning`). None → report and stop.
2. **Triage honestly** — real / already-fixed / not-a-defect. Read the target file and git
   history first; don't manufacture work.
3. **Draft a changeset** — concrete edits to specific skills/docs/commands in this project, tied
   to findings.
4. **Present the full changeset for approval. Apply/commit nothing first** — even "obvious,
   reversible" edits go in the changeset, not straight to disk. `--dry-run` stops here.
5. On approval: apply, commit (standard trailer), then **`mem_delete` every processed finding
   (implemented OR rejected) and save nothing in its place** — git is the only record.
