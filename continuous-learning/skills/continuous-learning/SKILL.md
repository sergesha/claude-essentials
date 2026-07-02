---
name: continuous-learning
description: Use when capturing a finding mid-work (a tool, API, or workflow just behaved unexpectedly, or you found a materially better approach), when wrapping up any task (the learn: checkpoint), or when running /learn to turn accumulated findings into this project's own skills/docs/commands. Triggers — "capture this", "note this for later", "/learn", "what have we learned", "promote findings".
---

# Continuous Learning

Capture surprises while working, then periodically promote them into this project's own
versioned docs. **Memory holds only OPEN findings; git is the only record of what was learned
and fixed.** A finding's presence in memory *is* its "open" status — there is no other status,
and nothing stays in memory once it's handled.

## Capture

**When**: mid-task (a tool/API/MCP just surprised you, or you found a materially better
approach) and at the end of every task (the checkpoint — `learn: none`, or `learn: [slug]
surprise → what to change`).

**What qualifies**: a tool/API/MCP surprise, a wrong or missing doc, a needed workaround, or a
materially better approach. **Not**: routine successes, expected outcomes, files you created.

**Format** (not freeform): `[slug] surprise → what to change`. One line. No status fields —
presence in memory is the only status there is. Never include instance specifics (hostnames,
IPs, paths, tokens) — abstract to technique. Pass the bare slug as `label` too (not just inside
`text`) — `mem_list`'s compact view truncates `text` at a fixed length, so an explicit `label`
is what keeps the finding scannable during triage instead of cut mid-sentence.

**Tag** (mandatory): every finding destined for `/learn` MUST carry the exact tag
`continuous-learning` — that fixed tag is the only thing `/learn` searches by. Topic tags are
fine *alongside* it, never *instead of* it.

**Before saving**: `mem_search` for the same finding first — dedup, don't create near-duplicates.

Best-effort: if memory is unavailable, skip silently — never block the task on it.

## `/learn` — promote findings into this project

1. Load open findings (`mem_list`/`mem_search` tag `continuous-learning`). None → report and stop.
2. **Triage honestly** — real / already-fixed / not-a-defect. Read the target file(s) and git
   history first; don't manufacture work.
3. **Draft a changeset** — concrete edits to this project's own skills/docs/commands, each tied
   to a specific finding.
4. **Present the full changeset for approval. Apply nothing first** — even "obvious, reversible"
   edits go in the changeset, not straight to disk.
5. On approval: apply, commit (standard trailer), then **`mem_delete` every processed finding —
   implemented OR rejected — and save nothing in its place.**

## Lifecycle

| State | Action |
|---|---|
| **Open** | `mem_save`, tagged `continuous-learning` — surfaced by `/learn` |
| **Processed** (implemented *or* rejected) | `mem_delete`. Nothing replaces it. Git is the record. |

## Rationalizations — STOP

| Excuse | Reality |
|---|---|
| "I'll tag it by topic instead (`foo-cli`, `gotcha`, ...) — more useful for search." | Topic tags are fine *in addition*, never *instead of* `continuous-learning` — without that exact tag, `/learn` never finds it. |
| "I'll write the full story, not just the terse line — more context is better." | The one-line `[slug] surprise → what to change` is for scanning many findings at triage time. Put detail in the `code`/body field if truly needed, not the headline. |
| "I'll save a `resolved`/`validated`/`done` record so memory reflects reality." | No. Processed = deleted. The commit is the record. A resolution record re-pollutes `mem_search` and rebuilds the stale-memory dependency this workflow deliberately avoids. |
| "The hostname is just a 'confirmed on' data point, not a key fact." | Still instance-specific. Findings carry zero instance specifics. Abstract it. |
| "These edits are low-risk and reversible — I'll just apply them." | `/learn` drafts the whole changeset and gets approval before applying or committing. No self-apply. |

## Red flags

- About to `mem_save` with only topic tags, missing the `continuous-learning` tag.
- About to `mem_save` a paragraph instead of the one-line `[slug] surprise → what to change`.
- A finding names a host/IP/domain/path — even "confirmed on …".
- Applying or committing `/learn` edits before the full changeset was approved.
- Keeping a handled finding "for the record."

All of these mean: fix the tag/format, strip the specifics, or delete the processed finding and
save nothing in its place.
