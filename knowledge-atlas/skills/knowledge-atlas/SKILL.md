---
name: knowledge-atlas
description: Use when working with Knowledge Atlas sources, candidate or type review, evidence search, knowledge application, feedback, package composition, or archive recovery.
---

# Knowledge Atlas

Use the installed `knowledge-atlas` MCP tools. Discover their current input
schemas first. Tool names are `atlas_` plus the action with dots replaced by
underscores: `candidate.inspect` is `atlas_candidate_inspect`; `atlas.status`
is `atlas_atlas_status`. Omit `payload` for calls with no payload fields.

## Operate

1. Open the configured atlas with `atlas_atlas_open` without arguments. When the
   user names a Compose project, pass `{"payload":{"project":"PROJECT"}}` on open;
   subsequent tools use that project on this connection. Use an explicit path
   for an unbound local service. Retain the returned `atlas_id`. Read its
   current READY publication before preparing a mutation. Keep the user's
   source IDs stable; omitted sources remain, explicit removals are versioned.
2. Submit with a fresh request UUID and the reviewed `expected_publication`.
   Poll `atlas_operation_read` using the returned operation ID. A succeeded
   ingestion produces a candidate, not accepted knowledge.
3. Inspect the exact candidate. For example:
   `{"atlas_id":"ATLAS","payload":{"candidate":"EXACT_OID"}}`.
   Present material changes, provenance, unresolved assertions and disputed
   attribution before knowledge acceptance. Match existing user approval to
   that material. A relation-type approval applies to the catalogue decision;
   knowledge acceptance remains separate. Resolved material can proceed while
   unresolved assertions remain visibly unaccepted.
4. Accept/reject/revise through the corresponding tool under the user's
   applicable authority. Use the request UUID as the knowledge decision ID; supply actor, reason and
   time. Type decisions have their own catalogue decision ID. Use
   synthetic attribution for verification; caller-supplied `human` is not
   identity proof. A stale base requires renewed inspection. Acceptance may
   return accepted content with publication still pending: report both.
5. Read/apply the selected publication. Report its actual status, grounds,
   provenance, unknown conditions and conflicts. Ranking scores are retrieval
   scores. Technical failure is not absence of knowledge. Use `as_known_at`
   for acceptance history and `valid_at` for applicability; unknown validity
   stays unknown. Apply structured knowledge entries from reviewed revisions,
   feedback or composed packages. A `retrieval_only` source, wiki page or
   extracted statement can supply evidence. When answering questions, report
   partial or insufficient coverage even if retrieval returned citations.

## Follow-up actions

| Need | Action and result |
| --- | --- |
| Correct or withdraw knowledge | `knowledge.revise` proposes a candidate; inspect dependent impact and review it. |
| Record experience | `feedback.record` retains the exact applied publication and raw JSON report text. |
| Compose knowledge | `packages.pin` proposes an exact child pin; upgrades and rollback require candidate review. |
| Recover history | Export then restore to an unused location; restored reads require native rebuild. |
| Interrupted work | Inspect operation/evidence, then explicit compatible resume; uncertain effects are not automatically replayed. |

Configuration and prepared-runtime setup: [installation](../../docs/installation.md).
Request examples and result meanings: [API](../../docs/api.md).
Archive prerequisites: [archives](../../docs/archives.md).
Processing belongs to backend tools; the skill does not reproduce the pipeline.
