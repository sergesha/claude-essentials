# redis-memory-mcp server

Native Python stdio server for the
[`redis-memory`](https://github.com/sergesha/claude-essentials/tree/main/redis-memory-mcp)
plugin.

Install this directory with Python 3.11 or newer, set `REDIS_URL` and
`EMBED_URL` for an accessible Redis Stack and embeddings service, then run
`redis-memory-mcp`. The executable starts only the MCP bridge; it does not
provision backend services or invoke Docker/Podman.

See the [plugin README](https://github.com/sergesha/claude-essentials/tree/main/redis-memory-mcp)
for configuration, namespace, ACL, and container-based installation details.

## Gemma Q4 and migration

Install `.[embeddings]` to run the shared CPU model service with
`EMBED_PORT=8082 redis-memory-embeddings`. Point bridges at
`EMBED_URL=http://127.0.0.1:8082`. Model weights are downloaded on first start;
review the bundled `licenses/GEMMA-TERMS.txt`, `GEMMA-PROHIBITED-USE.txt`,
`MODEL-USE-TERMS.md`, `NOTICE` and `THIRD-PARTY.md` first. These documents are
included in the wheel. The plugin's MIT license does not replace the model terms.

`CHUNK_MAX_TOKENS=256` and `EMBED_DIMENSION=256` are defaults. Supported chunk
budgets are 64–2048 tokens; dimensions are 128, 256, 512 or 768. Chunk budgets
include the bounded label prefix and special tokens. Overlap is 32 tokens.
Changing either setting for existing data requires offline re-embedding; all
clients accessing an area must agree on the profile.

Use `redis-memory-migrate` for explicit build, verification, cutover and cleanup.
Follow the [migration runbook](https://github.com/sergesha/claude-essentials/blob/main/redis-memory-mcp/MIGRATION.md),
including verified backup and stopping every old/new client. Do not change the
endpoint of an old client in place. TTL refresh on ordinary reads is intentional.
