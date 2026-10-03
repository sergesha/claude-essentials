# One-off migration to Gemma Q4

Existing vectors are incompatible. **Do not simply replace the embedding endpoint.**
Canonical memory IDs, text, code, labels, tags and timestamps remain unchanged.
KV does not need migration. Reads intentionally refresh TTL; migration itself
preserves the original absolute expiry and permanent records.

The same procedure handles a later change to `CHUNK_MAX_TOKENS` or
`EMBED_DIMENSION`. Every participating MCP client must use the same profile for
an area. There is no automatic migration from MCP tools or at startup.

## Prepare and inspect

1. Inventory all clients, including old Docker containers/native processes.
2. Take a consistent Redis-native RDB backup in a private directory (mode700,
   file600), record its SHA256 and successfully restore it into an isolated
   Redis Stack with the same modules. Verify canonical/KV payloads and expiries.
   Copying a live AOF file is not a verified backup. Preserve the backup outside
   the application cache and Git. Make a fresh backup before the real run.
3. Start the new shared Gemma service on8082 alongside the old endpoint.
   Keep the old endpoint available until the rollback boundary below.
4. Install the new wheel/source (`pip install ./redis-memory-mcp/server`) in an
   admin environment. Set `REDIS_URL` through the environment; never put an
   authenticated URL in a process argument. Cross-area work needs admin ACLs.
5. Inspect the selected area, without changes:

```bash
export EMBED_URL=http://127.0.0.1:8082
export CHUNK_MAX_TOKENS=256
export EMBED_DIMENSION=256
redis-memory-migrate inspect
# Administrative inventory of all recognized namespace/scope areas:
redis-memory-migrate inspect --all
```

Default selection follows `NAMESPACE`; `--shared` and `--scope NAME` have the
same meaning as MCP arguments. `--all` scans only recognized canonical UUID
memory records, not KV or derived chunks. The manifest contains private area
names and IDs; do not publish it. Standard output contains aggregate counts only.

## Offline build, resume and cutover

Stop **every** participating old/new client before continuing. Legacy clients
ignore the new lock. Set Redis `maxmemory-policy noeviction` administratively for
the migration, ensure sufficient memory for old+new indexes, and retain the
previous policy for restoration later. The migrator refuses unsafe memory
headroom and detects any evictions; an OOM must be resolved before resume.

The flags below attest that clients are stopped and the supplied backup has
actually been restored and verified. They do not perform those steps for you.
Use the same original backup, manifest, area selection and profile on resume.

```bash
redis-memory-migrate build --all --manifest /private/migration/run.json \
  --backup /private/migration/dump.rdb --backup-verified --quiesced
redis-memory-migrate verify --all --manifest /private/migration/run.json \
  --backup /private/migration/dump.rdb --backup-verified --quiesced
redis-memory-migrate cutover --all --manifest /private/migration/run.json \
  --backup /private/migration/dump.rdb --backup-verified --quiesced
```

An interrupted build resumes with the identical build command. Complete records
are verified and reused; incomplete generations are rebuilt without exposing
partial writes. Missing/expired parents are never recreated. Changed live source
or expiry, a lost lock, wrong profile, missing index entries or eviction blocks
cutover. Diagnose using the private manifest and local backend; do not delete a
manifest or reset an area's state to bypass an error. A process failure releases
its lease after at most120seconds. The area remains unavailable until recovery.

After successful cutover, start all clients on the new version/profile/endpoint.
Check save/search/list/delete and area isolation before reopening normal traffic.
Then restore the normal Redis eviction policy. The migration does not stop/start
clients or change the Redis eviction policy itself.

## Rollback boundary and cleanup

Before reopening writes, the preserved old vectors and verified backup permit
operator rollback to the old clients/endpoint. New clients must be stopped first;
restore the backup into an isolated instance and switch back only after verification.
For a previous chunked profile, restore its matching state/index/endpoint together.
A backup restore rewinds all data to the snapshot and may lose later changes.
After new writes resume, the old index is stale: it is NOT a lossless instant rollback.

Cleanup is explicit and irreversible for old vectors, so keep the backup. Stop
clients for maintenance and run:

```bash
redis-memory-migrate cleanup --all --manifest /private/migration/run.json \
  --backup /private/migration/dump.rdb --backup-verified --quiesced
```

Cleanup validates the new active profile, drops only the recorded old indexes
(without `DD`), removes old vector fields/derived keys, and preserves parent/KV
records. It can be repeated. Retire the old embedding container only after the
rollback decision. A new profile migration uses a new manifest and fresh backup.
