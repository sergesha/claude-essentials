# Portable archives

`archive.export` writes a new ZIP at payload `path`, using the registered atlas
and its current `expected_publication`. `archive.restore` takes payload `archive`
and an unused destination `path`. Both are durable worker operations. Submission
retains a private copy of restore input bytes in attempt evidence. Use a regular
file with a single hard link outside service and atlas directories, for example `/data/exports/atlas.zip`
in Docker. Keep the compressed file within 512 MiB. The service checks the ZIP
directory and requires an `archive.json` entry before accepting a restore. Queued,
running and interrupted restores share a 1 GiB staging budget. When the budget
is full, submission returns HTTP 507 (`archive_staging_full`). A worker deletes
its private input after success or failure and retains the operation record,
diagnostics and caller's original file. At startup and during admission, the
service reclaims copies left by terminal attempts and abandoned submissions.

The restore limits are
1,024 ZIP entries, 512 KiB of central directory metadata, 256 MiB per member,
8 MiB for `archive.json`, and 1 GiB across all expanded members including the
manifest. Each filename must occur once, and the hash inventory must match the
package bundles and runtime assets. Export checks the same limits before making the destination file visible.

Format 3 includes Git bundles, hashed runtime assets and a versioned decision
inventory keyed by the UUID of each bundled atlas. A project with no decisions
has an empty list. Source and knowledge leaves carry their own Git history.
Every bundled revision requires integer `atlas_format: 3` in `package.json`.
Restore verifies the decisions, child selections and publication vectors in
staging. The service uses the qualified formatter and tokenizer to reconstruct
the distinct document inputs for each publication and verifies their retained
vectors before exposing the destination. It accepts only format 3.

An export is a full-history backup. It can include additional child history,
unpublished proposals and records beyond the selected parent publication.
Every reviewed child inclusion carries its exact publication wrapper and content
commit, including nested selections and earlier parent layouts. Child READY may
have moved. Restore resolves these bindings by UUID inside the bundled repositories.
Original project paths, submodule checkouts and remote access are unnecessary.
A missing wrapper or a mismatch between its reviewed content, UUID and gitlink
causes restore to fail before the destination becomes visible.

Restore validates hashes and package/decision closure before exposing canonical
files. It then reuses PoC9's oldest-to-newest native projection rebuild, verifies
profile, counts and vector artifacts against each original publication, and
atomically writes `rebuilt-projections.json`. Canonical publication OIDs and
READY remain unchanged. The service registers the restored atlas as ready only
after rebuild succeeds.

Archive-provided hashes establish internal consistency, not authority to run
code. Projection requires the embedding profile to equal the independently
packaged qualified profile and checks restored tokenizer/formatter digests
against that profile. Only the packaged formatter is executed; an archive
cannot authorize its own Python formatter by supplying a matching hash.

A rebuild failure retains the restored canonical files and failed attempt
evidence. The destination is never overwritten. Correct missing prerequisites
and restore the same archive into another unused destination. Automatic replay
of an interrupted restore requires a registered recovery handler. The standard
service returns HTTP 409 (`unsafe_resume`) and preserves the original attempt
and staged input. Inspect the canonical result before starting a fresh restore.
If interrupted inputs fill the staging budget,
confirm the operation is interrupted, preserve a copy of the input outside
the service, and remove only that operation's staged `archive-v3.zip` before
submitting the fresh restore.

The prepared runtime must have the pinned tokenizer, formatter, native graph
and embedding bridge configured against this restored atlas's vector cache.
Retained document embeddings replay without inference; a fresh query still needs
the matching live Gemma endpoint. Prepared-runtime recovery does not demonstrate
clean-machine installation or retrieval quality.

Native qualification runs with:

```sh
uv run pytest tests/integration/test_archive_runtime.py -q --runtime-config CONFIG.json
```

The JSON selects the prepared `runtime_root`. With an optional compatible
`archive`, the test replays its current and historical publications. With
only the runtime configured, it creates two reviewed parent publications
pinning different child versions, exports the parent, and removes its temporary
source repositories before recovery. Recovery uses a fresh graph database
and the restored vector cache, verifies both publication slices, checks zero document
inference during replay, and runs one fresh query inference.
