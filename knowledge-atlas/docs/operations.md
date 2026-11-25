# Atlas operations

The service keeps operation bookkeeping in its external data directory. Canonical atlas identity, content, decisions and publication history remain in PoC9-compatible Git packages. Deleting an operation database does not delete canonical knowledge; portable archive recovery is a separate operation.

`atlas.create` requires an unused external destination and returns its new UUID.
`archive.restore` registers the recovered UUID after projection replay succeeds.
`atlas.list` lists registered projects and their READY states. `atlas.open` verifies
the registered repository for the supplied UUID. `atlas.status` separates READY
publication, accepted content, pending candidates and active operations.

Mutations require a client-generated UUID `request_id` and the expected READY publication (null for an empty atlas). The service verifies the project and freezes its repository, graph binding and effective configuration before queueing. Repeating an identical request UUID returns the original operation; changing its payload returns `request_conflict`. This deduplicates submission, not remote model billing.

Operations transition from `queued` to `running` and then `succeeded`, `failed` or `interrupted`. Domain results such as `pending_review` and accepted-but-not-published content are distinct from operation state. Each attempt retains its request/configuration, result or error, and stage evidence in a new directory. Input source content is part of the frozen JSON request; callers supply text, not a mutable file reference.

A child worker holds the service writer lock and canonical atlas lock throughout mutation. Alias paths resolve to the same writer lock. A conflicting service receives `busy` rather than stealing an active lock. The dispatcher runs one mutation at a time across all projects. Reads continue against each project’s available READY while work runs. Before calling a handler, the worker verifies that the queued repository UUID, location and graph binding still agree with the registry. Client disconnection does not cancel accepted work.

After restart, an active attempt lock keeps an operation running; an inactive attempt becomes interrupted. No model call is automatically replayed. Explicit resume requires a new request UUID, an inactive attempt and handler-specific canonical reconciliation. It uses a fresh workspace and refuses `unsafe_resume` when safe continuation cannot be established. Completed, compatible stage artifacts may be copied by the domain recovery handler; an old agent workspace is never reused blindly.

Explicit shutdown reports queued/running operations as `busy`. A caller may wait for the queue to drain. Forced process termination can interrupt work; lock liveness is authoritative rather than a potentially reused PID. There is no claim of arbitrary-kill recovery or exactly-once model billing.

The operation engine, public service transports and domain actions are implemented. Native host/platform qualification is recorded separately from deterministic operation tests in [verification](verification.md).

## Explicit knowledge decisions

`candidate.inspect` returns the selected candidate, its base snapshot, source text,
provenance, unresolved relations and grouped type questions. `candidate.decide`
requires `candidate`, `action` (`accept` or `reject`), `actor`, `kind` (`human` or
`synthetic`), `reason` and timezone-aware `decided_at`, in addition to the common
request UUID and expected READY. The UUID is the durable decision ID.

Decisions live in immutable `refs/knowledge-atlas/decisions/<UUID>` commits with
`atlas-decision.json`. Each receipt retains the candidate tree and package closure;
its parents keep the candidate, reviewed base and accepted/revised result reachable.
Rejection records a decision against that candidate. A fresh decision UUID can subsequently accept it while its base remains current. Both receipts remain in history.
Inspection survives loss of the service database. Caller-supplied attribution is
recorded as supplied; a `human` flag is not proof of human identity.

Acceptance uses PoC9's existing review path, then its projection/publication path.
The result identifies accepted content separately from READY. A projection failure
returns `accepted_pending_publication`, its error and the previous READY, which
remains unchanged. Explicit `operation.resume` with a fresh UUID may continue this
otherwise succeeded operation: canonical reconciliation reuses the existing receipt
and accepted content before attempting projection. A lost acknowledgement after
publication resolves to the existing publication, including when later history has
advanced. No duplicate acceptance or automatic model retry is implied.

Each new candidate records its base publication, including an explicit null for the first candidate. Regenerate and inspect a pre-upgrade candidate that lacks this binding. A stale base returns `stale_proposal` or `stale_publication`. Backdated acceptances return `invalid_acceptance_time`. Earlier source versions that lack a per-source recording time use the retained Wiki compilation time where available.
If an older release accepted a far-future date, submit the next review against the
current READY with the correct current time. The service uses the old publication's
own `published_at` as its acceptance time when the claimed date exceeded that clock by
more than five minutes. The original Git receipt and publication ID remain available.
For a future-dated review still pending publication, issue a fresh acceptance with
the corrected time against the current base.

`candidate.revise` creates a new candidate with attributed receipt, using either
`entries` for explicit structured changes, or `sources`, `remove_source_ids` and/or
`instructions` through the retained native Wiki/Utopia path. It also requires actor,
kind, reason and decided_at. The original OID stays immutable. If an interrupted
revision has no completed canonical receipt, resume refuses `unsafe_resume` rather
than repeat an uncertain model effect; retained attempt evidence must be inspected.

Projection uses the prepared runtime tokenizer and formatter. Configure its local
native endpoint with `graph: {"url": "http://127.0.0.1:<port>"}`. The registry
assigns each new project a unique database derived from its UUID and reopens that
exact binding on restart. A registry with missing or ambiguous ownership stops
startup before workers run. Conflicts during project selection produce `graph_atlas_conflict` before a native projection write.
The embedding bridge and builder must share a compatible cache. Native runtime
and host qualification are recorded separately from controlled projection tests.

## Docker storage durability

Docker defaults to persistent named volumes; optional host mounts and full-backup
responsibilities are described in [the storage guide](../docker/README.md#storage-ownership-and-optional-host-folders).
Treat TerminusDB, accepted model results and pending-work evidence as durable.
An LLM rerun is regeneration, not restoration of the same accepted knowledge.
Deleting a service database loses operation bookkeeping even if canonical Git
knowledge survives; canonical exports are not a full deployment backup.

## Large source preparation

The pinned embedding profile limits each canonical record to 256 chunks of up to
256 tokens. A large article supplied as one source can exceed that limit even
when sufficient RAM is available. Before ingestion, preflight source text with
the qualified tokenizer/formatter. Partition oversized text into ordered source
parts, preserve exact text and provenance, and verify concatenation against the
original. Include each part's stable ID, source URL/version, full-document hash
and character offsets. Do not silently truncate or change the qualified profile.
Re-ingesting partitioned input generates a new candidate; it does not restore
an earlier model-generated version.

A large first index can outlast the builder's 60-second readiness deadline.
Inspect index progress and the retained publication error. Once the exact index
has completed, use explicit `operation.resume` on the original accepted operation;
do not submit another acceptance. The large-survey demo exercised this path.

When attaching another atlas to an existing demo embedding bridge, its configured
artifact cache still belongs to the original atlas. The builder requires retained
artifacts under the target atlas's `vectors` directory. This demo used SQLite's
online backup API to copy the completed cache before resuming, and the normal
builder verified every current chunk against its profile artifact and native
vector. This is a manual deployment step, not automatic multi-atlas cache routing.

## Native LLM Wiki compilation

Wiki authoring uses the pinned upstream LLM Wiki MCP tools and its
[guide](https://github.com/lucasastorian/llmwiki/blob/aac3e6493306b7fff3b09cb181ca93918e93d0e6/mcp/tools/guide.py).
The connected Codex or Claude agent authors the pages. Both adapters load the
same operational prompt from `runtime/wiki-instructions.txt`: call the
native guide and follow it for the requested ingestion/update. The prompt explicitly requests the guide's maintenance workflow, including
source-grounded semantic review after lint and reference-impact reconciliation.
It adds no page structure, depth quota, entity list or extraction schema.
The upstream guide is unmodified.

The remaining integration instructions identify a candidate workspace, treat
source content as evidence, and require completion/blocker metadata. If the
native workflow needs clarification, the run reports a blocker rather than
inventing an answer. The native guide includes discussing takeaways with the
user; headless execution is not a replacement for that interaction. Explicit
source removals carry their reconciliation request. Publication still requires
the existing separate inspection and acceptance flow.

For direct native use, connect the workspace over MCP and ask:

> Read the native Wiki guide and ingest the sources into this Wiki.

A consolidated glossary is not a mandatory default page. The native guide
requires concept definitions and entity facts, and permits additional useful
pages. Atlas adds no glossary requirement, fixed page count or extraction schema.
The existing `wiki.guidance` profile field accepts optional task instructions,
just like a request to the external native MCP client. Empty guidance remains the
default. It is not an upstream guide override or a native prompt-editor setting.
For example: `"guidance": "Maintain a concise orientation wiki. Verify the claims against their source passages and repair incorrect citations using the native tools."`
Model and effort remain configurable. Guidance is included in the stage profile
identity, so changing it prevents reuse of an incompatible prior result. The shared operational prompt is included in both
providers' execution identities, so changes invalidate frozen execution.

Use the native `read` tool's `sections` parameter when a host truncates a large
response. Section names must match exactly (case-insensitive); a selected parent
heading does not include nested subsections. Read the child headings or search
before concluding that content is absent. Text-source citations can use the full
filename alone; do not invent PDF page numbers to satisfy the citation parser.
A complete retained source does not prove that the model saw every passage. `lint` checks citation/link/metadata hygiene; zero errors does not prove
substantive coverage. Inspect the actual explanations and source support as the
native guide requires. Do not equate a larger number of pages with completeness.
This remains model-driven authoring, so detail and organization can differ between
runs and providers. No custom output validator, page generator or glossary schema is added.

Previously generated demo pages retain the prompts and identities used to create
them. Upgrading the adapter does not regenerate or relabel those publications as
native-default output. Historical qualification using the richer Atlas prompt
is not evidence of default-only authoring behavior.


For precise online evidence, supply verified version-specific source URLs in the
request guidance or source material and ask for ordinary Markdown links beside
native filename footnotes. Do not put the deep URL inside a native footnote: the
upstream parser retains its label, not its URL. Never invent paragraph IDs.
`read(sections=...)` is a reading filter, not a citation anchor. PDF page citations
and highlights are separate native mechanisms. The Atlas viewer currently opens
external fragment links but does not provide paragraph navigation inside retained
local text files. A model's completed status and summary are evidence to inspect,
not a deterministic guarantee of semantic correctness.
