# Local viewer

The viewer reads the same accepted publications used by Codex, Claude Code and
Python clients. It provides wiki browsing, exact publication comparisons,
evidence-cited questions, and clarification → inspect → explicit accept.

For a running Docker deployment named `atlas`, launch on the **host**:

```sh
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh view --atlas /data/atlas --docker-project atlas
```

For native deployment:

```sh
knowledge-atlas view --atlas /absolute/path/to/atlas --config /absolute/path/to/service.json
```

An installed Python wheel also supports the Docker command:
`knowledge-atlas view --atlas /data/atlas --docker-project atlas`.
The `--atlas` argument accepts a registered project UUID or an explicit repository path. With Docker, the viewer resolves the path inside the selected container.
The host needs Python/uv for the viewer and Docker CLI for Docker attachment.
The backend stays in its container; no container ports are published.

Open the complete URL printed by the command. It binds only `127.0.0.1`; optional
`--port 8765` chooses a stable port. The URL fragment is a fresh browser-session
secret, distinct from backend/provider credentials. The page removes it from the
address bar and keeps it only in memory. Reloading requires reopening the full
launch URL; restarting the viewer invalidates the old secret. Do not share it.
Stop the viewer with Ctrl+C; its backend atlas data remains intact.

## Explore and compare

Use the sidebar to select a knowledge version and find a page. **Read** is the default view; **Ask**, **Graph**, **History** and **Review changes** each have a dedicated screen. The page filter searches titles without changing the selected version. Select a page to return to reading.

Select a publication, then a wiki page. Historical pages and questions remain
bound to that exact version, even when current READY advances. The timeline shows
actual Atlas acceptance, with synthetic decisions labelled. Source revision and
actual ingestion dates appear separately in the selected publication metadata.
Unknown dates stay unknown. The selected publication and page identifiers remain
available under **Sources & publication details**. Wiki metadata headers are
retained in canonical content but omitted from the rendered reading view; known
wiki links navigate within the selected version. Local references that cannot be
resolved expose a provenance message instead of opening an unserved file path.

In **History**, choose the earlier version and select **Show changes**. Expand a
changed record to compare readable before/after text; exact records and provenance
remain available in a disclosure. The comparison displays exact canonical record
changes between two publications; it does not invent continuity across different
record IDs. Composed packages retain distinct page identities.

Open **Ask**, or choose **Ask a question** from the reading view. The configured `grounded_answer` provider
receives a bounded set of evidence from the selected publication. Citation buttons
open retained records and select them as clarification targets. A citation proves
which record was supplied, not that an answer's interpretation is correct. Missing
evidence, truncation and technical failures are explicit. Imported HTML/scripts
and external images are not executed or loaded.

## Propose, inspect, accept

1. Select the latest version and the page or citation needing clarification. Choose **Suggest a clarification**, or open **Review changes**.
2. Enter your recorded name, supporting evidence and clarification; choose
   **Propose clarification**. Processing may take several minutes.
3. Choose a saved proposal or paste its identifier, then select **Inspect changes**. Read the before/after text and the visible unresolved assertion/type-question counts. Complete snapshots and provenance remain available in disclosures; inspect them before deciding. Review grouped type questions. Type acceptance is separate from knowledge acceptance.
4. Enter a reason and choose **Accept & publish** or **Reject**.
   Attribution is supplied locally; the name field is not identity verification.

Historical publications are read-only. A changed READY invalidates an old reviewed
base. Published knowledge remains unchanged until explicit acceptance/publication.
Accepted-but-not-published results remain visible as failures to publish; use the
agent's existing operation-resume workflow after inspecting the cause.

If a response is lost, keep the tab open and choose **Recover pending operation**.
The viewer retains the same request ID and payload, recovers the existing operation
and does not submit a new deliberate decision. It blocks a second mutation while
one outcome remains uncertain. Do not refresh a tab with a pending decision;
if the tab was lost, inspect atlas/operation state through your agent before
submitting another intent.

## Scope

Local single-owner HTTP only. Remote/LAN access, HTTPS hosting, accounts, full wiki
editing, automatic acceptance and autonomous research browsing are deferred.
The real paper demonstration uses a separate atlas and clearly synthetic acceptance;
it is not a claim that paper contents or extracted knowledge were human-validated.

## Try a versioned source

Use an isolated atlas and a document with two explicitly identified revisions.
Retain each original source, its URL, revision/date and content hash when ingesting.
Do not confuse the document's publication date with the date you accept knowledge.

1. Ingest the first revision through your agent, inspect the candidate and decide
   explicitly whether to accept it. Open the viewer with the command above.
2. Ask a factual question whose answer is present in the source. Open its citations
   and check the evidence rather than relying only on the answer text.
3. Ingest and review the second revision using the same stable source identity.
4. Use **History** to select and compare the two publications. Ask the same question
   in each. Earlier publications are read-only and remain tied to their evidence.
5. Propose a clarification, inspect its changes and evidence, then explicitly accept
   or reject it. Unresolved assertions and type questions are separate decisions.

For a larger example, [Agentic Reasoning for Large Language Models,
v2](https://arxiv.org/html/2601.12538v2) was used during qualification. A maintained
seven-page Wiki and an earlier 13-page publication were checked against the same
retained source parts. Those demonstration datasets and pending proposals are not
bundled with the plugin. Your generated pages and identifiers will differ.

See [verification and limits](verification.md) for the actual scope of the native
maintenance and browser checks. A shorter Wiki can be a useful orientation, but
page count alone does not establish source coverage or factual correctness.

## Explore the evidence graph

Choose **Explore connections** on a wiki page, **Show in graph** on an opened
answer citation, or open **Graph** and search for a concept. Search uses the
selected publication's full-text index; it makes no LLM call. Pick a result to
start a new local neighborhood.

Select an object to inspect it. Select a connection to see its actual direction,
type definition, polarity and available conditions, with the underlying evidence
record. **Expand neighbors** adds one hop without moving existing objects;
**Start here** resets the map around that record. **Fit to view** brings the
loaded neighborhood into view. Drag to pan, scroll to zoom, or use the
**Objects & connections** lists to select items with the keyboard.

Dashed **Reference links** are stored navigation/provenance links, not assertions
of support or causality. Turn them off to isolate semantic relationships. Solid
arrows represent supplied resolved relationships; unresolved records retain their
actual status in the inspector. Publication acceptance, including synthetic demo
acceptance, is not a guarantee of truth. No confidence score is invented.

Use **Read wiki page** or **Follow recorded references** to inspect the actual
record in the selected publication. Source records expose their full retained
text. A saved provenance citation with exact character offsets and a matching quote
can be opened with **Inspect cited fragment in this version**; the matching text
is highlighted, with Unicode codepoint offsets preserved. If the locator is absent
or fails verification, the inspector explicitly identifies the whole-document
fallback; it does not guess fragment anchors. Original payload,
source commit references and available provenance remain under **Exact record &
provenance**. A reference shown in the current publication is not a reconstruction
of a different historical source commit; inspect the retained provenance for that
original pin.

Changing the sidebar's knowledge version clears the graph, search results and
inspector. All new reads use that version; late old responses cannot repopulate
it. Pending proposals are not shown as published graphs.

Each neighborhood request returns at most 40 records; the displayed accumulation
is capped at 150 objects and 500 connections. Limit messages describe a partial
view, never proof that other relationships do not exist. The existing backend
reads the full selected TerminusDB projection before bounding the result, so this
release is qualified for the small demonstration atlases, not large-scale graph
traversal. Graph comparison overlays and arbitrary pathfinding are deferred.
Cytoscape.js 3.35.0 is bundled locally with its MIT license; no CDN or additional
service is needed.

The version timeline labels acceptance kind and channel as caller-supplied. Older
versions can have an unrecorded channel. Each version identifies whether a
matching decision receipt exists. The question panel identifies partial answers
and insufficient evidence separately.
