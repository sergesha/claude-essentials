# Portable archives

`archive.export` writes a new ZIP at payload `path`, using the registered atlas
and its current `expected_publication`. `archive.restore` takes payload `archive`
and an unused destination `path`. Both are durable worker operations. Submission
retains a private copy of restore input bytes in attempt evidence. Use a regular
file with a single hard link outside service and atlas directories, for example `/data/exports/atlas.zip`
in Docker. Keep the compressed file within 512 MiB. The restore limits are
1,024 ZIP entries, 512 KiB of central directory metadata, 256 MiB per member,
8 MiB for `archive.json`, and 1 GiB across all expanded members including the
manifest. Each filename must occur once, and the hash inventory must match the
package bundles and runtime assets. Export checks the same limits before making the destination file visible.

Format 2 extends PoC9's Git bundles and hashed runtime assets with a versioned
inventory of canonical acceptance, rejection and revision receipts. Receipt
references, referenced commits, recursive package pins, publication history and
compatible document vectors travel together. The service SQLite database and
host credentials are not required. Compatible format 1 archives remain readable;
restoration never invents decision receipts absent from their Git history.

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
of an interrupted restore is deliberately unavailable: inspect the canonical
result before starting a fresh operation.

The prepared runtime must have the pinned tokenizer, formatter, native graph
and embedding bridge configured against this restored atlas's vector cache.
Retained document embeddings replay without inference; a fresh query still needs
the matching live Gemma endpoint. Prepared-runtime recovery does not demonstrate
clean-machine installation or retrieval quality.

Native qualification runs with:

```sh
uv run pytest tests/integration/test_archive_runtime.py -q --runtime-config CONFIG.json
```

The JSON supplies `runtime_root`, a compatible `archive` with at least two
publications, `graph` (`url`, unused `database`), `embedding_port` (unused local
port), and `raw_embedding_endpoint`. The prepared native graph must use that
embedding port. The test starts and stops its own bridge, rebuilds and reads
current/historical slices, checks zero document inference, and then checks one
separate fresh query inference. Without this configuration it is explicitly
skipped; controlled fixtures do not qualify a native platform or host.
