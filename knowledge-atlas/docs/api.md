# Action contracts

The shared handler registry is the domain boundary. HTTP, MCP and Python clients use the same implemented backend; these examples describe action requests.

## Source ingestion and update

`sources.submit` takes the atlas UUID, a fresh request UUID, `expected_publication` and this payload:

```json
{
  "sources": [
    {"source_id": "trial-note", "name": "note.md", "content": "Observed result; cause unknown."}
  ],
  "remove_source_ids": [],
  "recorded_at": "2026-11-13T12:00:00Z"
}
```

Supply UTF-8 text or Markdown as `content`; arbitrary executable commands and mutable source-file references are not part of the action. Source IDs are stable across updates and name changes. Different sources can have the same display name. Internally they receive safe unique filenames, with original names and IDs retained in provenance. Imported filename collisions fail explicitly.

Optional `source_time`, `valid_from` and `valid_to` retain supplied source metadata. Omitted metadata preserves the selected previous value; explicit null clears it. Recording time does not supply unknown source or validity time.

Omitting a source preserves it. Explicit removal excludes the raw source from the fresh native workspace and asks the native Wiki agent to reconcile body text, summaries and frontmatter against remaining support. Native extraction and alignment then run against the resulting complete pages. Historical Git versions remain intact. A completed Wiki stage cannot be reused for an incompatible removal.

The result identifies an immutable `candidate` with `status: pending_review`, unresolved assertions and retained attempt evidence. It does not accept knowledge or publish READY. Native Wiki edits and unchanged Utopia parsing/alignment retain their separate responsibilities. Exact extraction reuse and compatible page updates use PoC9's retained profile/native-binary/codec identities and complete-current-page statement-reference behavior.

## Type decisions

`type.decide` takes a candidate OID, current `decision_id`, `action` (`accept`, `reuse` or `reject`), actor, reason and recording time. `reuse` names an existing `type_id`. It calls the retained catalogue decision and native realignment path, creating a new candidate. Shared disputed types remain grouped; admitting a type does not accept underlying knowledge. Unmapped assertions remain unresolved, and rejected assertions remain visible.

Actor labels and local access credentials are not proof of a human decision. Deterministic fixture exercises are test evidence, not human acceptance. Knowledge review and publication are separate actions.

Failures retain request, stage and model evidence. `runtime_unavailable` means the native runtime must be configured; incompatible runtime artifacts are explicit failures. Native subprocess stdout/stderr are retained under the attempt. No provider retry or fallback is implicit.

## Published reads and application

`wiki.pages`, `wiki.read`, `question.ask`, `search`, `evidence.read`, `knowledge.apply`, `history` and `diff` share PoC9's
publication-selected reader. Omit the selector for READY, supply a full
`publication` OID, or use timezone-aware `as_known_at` for recorded acceptance
history. `valid_at` evaluates applicability within that selected publication; it
does not select another publication. Unknown validity remains unknown.

Search accepts `query`, `mode` (`vector`, `fts`, `hybrid`), `count` (1–1000), optional
`eligible` record IDs and `purpose` (`research` or `application`). Eligibility is
applied before native ranking. An empty array returns `no_eligible_records` without
a global query. Distances are ranking values, not confidence or truth scores.
`no_matches` is distinct from `projection_unavailable` (503); unavailable historical
projections never fall back to current content.

Evidence reads require `record_id`. Optional `expand: true` selects native graph
expansion with `depth` (0–10, default 1) and `limit` (1–1000, default 100). Results
carry actual relation roles, paths, selected publication and explicit truncation;
absence is not proven by a bounded result.

Only structured knowledge entries from reviewed `knowledge.revise` or `feedback.record` proposals, including entries imported through package composition, qualify for deterministic assessment. Source, wiki and extracted statement or relation records remain available as evidence. Applying one returns `retrieval_only`. The `declared_opposition` field reports visible inbound and outbound declarations with their statuses. The `conflicts` field identifies conflicts established in the supplied context.

Application requires `record_id` and an object `context`. It returns deterministic
conditions, grounds, independent episodes, conflicts, unknowns and reasons. Statuses
include `supported`, `conditional`, `insufficient`, `inapplicable`, `conflict`,
`needs_review`, `withdrawn` and `retrieval_only`. Missing support does not imply
falsehood. Optional `explain: true` invokes the configured `grounded_answer` stage
using the retained PoC9 instructions and supplied published evidence. Its separate
`explanation` cannot replace the deterministic status or assessment. Citations must
refer to supplied records; model failure is explicit and never retried silently.

History accepts an optional `record_id`; otherwise it lists actual published
history. Diff requires exact `before` and `after` publication OIDs and optionally a
record ID. Different IDs do not create an inferred cross-ID narrative.

## Feedback and corrections

`feedback.record` requires the current expected READY plus `record_id`, exact
`applied_publication`, `actor`, timezone-aware `recorded_at`, and `raw_report` as
submitted JSON text. The report contains `case_id`, `description` and object
`context`, with optional outcome/time fields. The applied publication may be an
older retained publication: its exact claim text and content remain the outcome's
provenance while the proposal derives from current READY. Failure in one case does
not automatically refute a lesson.

`knowledge.revise` requires `record_id`, `actor`, `reason` and `recorded_at`. Supply
explicit `entries` for source-grounded record changes or a native correction with
`sources`, `remove_source_ids` and/or `instructions`. Native corrections also
require explicit `kind` (`human` or `synthetic`) for attribution. Mixed structured
and native forms are rejected rather than partially applied; the target must exist
in selected current content. Structured entries
retain PoC9's exact provenance validation, recursive grounds and withdrawal rules.
The result exposes a new candidate and explained `impact`/`wiki_impact`; accept it
through the normal knowledge-decision path. Previously published content remains
unchanged until and after the new publication is created.

## Shared HTTP, Python and MCP interfaces

The local service binds `127.0.0.1` on an available port. Read the current endpoint from
`SERVICE_ROOT/discovery.json` and token from `SERVICE_ROOT/token`. Both files are owner-only.
The token rotates on service start. The Python client handles discovery and service verification.

Custom clients first open an HTTP/1.1 connection to the discovered loopback endpoint and
send a credential-free `GET /v1/identity` with an `Atlas-Challenge` header containing
32 random bytes encoded as lowercase hexadecimal. Verify the 200 response body equals the
lowercase hex HMAC-SHA256 of the UTF-8 string `<endpoint>/<challenge>`, keyed by the
token text with surrounding whitespace removed. On that same open connection,
send the action with `Authorization: Bearer TOKEN`. Repeat discovery and verification
when reconnecting. Keep tokens private. The service rejects browser origins.

- `GET /v1/schema`: generated action-specific request models.
- `POST /v1/actions/ACTION`: JSON request; 202 for durable mutation, 200 for read.
- `GET /v1/operations/ID`: observed operation, result or retained failure.
- `GET /v1/status`, `POST /v1/stop` with `{}`: explicit service lifecycle.

Read the generated schema instead of maintaining another field catalogue.
All action requests include `payload` (use `{}` for an empty payload). Mutations
carry a request UUID and expected publication; repeated identical requests return
the same operation. Invalid input is 400, missing credential 401, missing entity
404, conflict/stale/busy 409, unsupported input/configuration 422, and unavailable
service/runtime/projection 503. Asynchronous failures appear in operation state.

Python uses the same service:

```python
from uuid import uuid4
from knowledge_atlas.client import AtlasClient
from knowledge_atlas.api_models import Location, Payload

client = AtlasClient('/absolute/path/to/service')
operation = client.atlas_create(
    Location(path='/absolute/path/to/new-atlas'), request_id=str(uuid4()))
observed = client.operation(operation.operation_id)
# Poll while queued/running. Only use result after succeeded; inspect error otherwise.
if observed.state == 'succeeded':
    status = client.atlas_status(Payload(), atlas_id=observed.result.data['atlas_id'])
```

Typed payload models and `AtlasClient` wrappers share the HTTP schema model
catalogue. `client.call(ActionRequest(...))` is also available. MCP tool names are
`atlas_` plus dotted actions with dots replaced by underscores. Their input
schemas come from `GET /v1/schema`; results retain the same data/publication,
evidence and canonical effects. MCP error results preserve `code` and `status`.

Knowledge acceptance/rejection/revision uses the request UUID as its canonical
decision ID. `type.decide` has a separate native catalogue `payload.decision_id`.
Feedback context belongs inside the retained `raw_report` JSON text; no separate
context field is accepted and silently discarded.

## Wiki pages and questions

`wiki.pages` takes an empty payload and returns visible canonical page IDs and
source metadata for the selected publication, with separate ingestion and
acceptance dates. `wiki.read` takes `page_id` from that list and returns exact
page content. IDs retain package-composition identity; paths are not file access.

`question.ask` takes `question` (1–4096 characters) and optional `count` (1–10,
default 5). It uses the configured grounded-answer provider with at most 48,000
characters of retrieved evidence text/payload and reports truncation explicitly.
No hits means insufficient evidence without an LLM call. Citations must name
supplied evidence; technical failures remain errors. Historical selection never
falls through to current READY. The Python client allows 900 seconds for this
read. Raw question/model evidence remains in the private service data directory.

Publication-level `history` returns timeline metadata and publication/content commit identifiers.
It omits raw `vector_artifacts` and `native_export_excluded_ids` from the response;
those projection payloads remain intact in the Git-owned publication receipt.


### Review and answer metadata

Publication history and the selected wiki expose the caller-supplied acceptance kind and
channel. Older publications report an unrecorded channel. The `matching_decision_receipt`
field indicates whether a retained decision agrees with the publication. A missing receipt
is valid for pre-receipt history. An unsigned matching receipt establishes internal
consistency. Before serving a publication, the reader checks that its Git parents
identify the previous publication and the reviewed content. It also checks that
the accepted content descends directly from the candidate named in the review
and that the publication metadata agrees with that review. A publication that points to a
pending candidate fails this check, including during archive restore.

`question.ask` returns `coverage` as `answered`, `partial` or `insufficient`. Insufficient
coverage returns `status: insufficient_evidence` even when retrieval found records.
Synchronous provider failures return HTTP 503 with `provider_failed`.

`atlas.status.candidate_statuses` maps candidate commits to review status. Rejection and
subsequent acceptance are distinct retained decisions. A stale candidate must be regenerated
against the current publication. New acceptances preserve chronological ordering along the
publication chain. Acceptance checks run both when recording a review and before building
its projection. A timestamp more than five minutes ahead of the service clock returns
`invalid_acceptance_time`. For `candidate.decide` with action `accept`, this timestamp
comes from `decided_at`.

On older history, `as_known_at` selects the latest publication whose entire chain was
eligible by the requested instant. If a legacy acceptance was more than five minutes
ahead of its own `published_at`, selection uses that publication time. If a child was
accepted earlier than an ancestor, it becomes visible with that ancestor. History and
`wiki.pages` preserve the caller's `accepted_at` and include `effective_accepted_at`
when the two instants differ.
New reviews and pending publications must meet the base publication's effective
cutoff. The service derives it from Git history and caches it locally for the
exact publication ID. A restored atlas can reconstruct it from the archive.
