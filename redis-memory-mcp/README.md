# redis-memory-mcp

> Persistent cross-session memory for AI agents — semantic search + KV store with auto-expiry

Long-term self-managing memory for LLM agents (Cursor, Claude Code, etc.) via [MCP](https://modelcontextprotocol.io).

This is one of the plugins in the [`claude-essentials`](../) marketplace; see there for install
instructions. It moved here from the standalone `sergesha/redis-memory-mcp` repo at v0.5.0 — that
repo now only redirects `start.sh` here, see its README for details.

## Features

- **Semantic search** (`mem_*`) — save facts with vector embeddings, find by meaning
- **Key-value store** (`kv_*`) — instant O(1) lookup for named facts
- **Auto-expiry** — TTL resets on every read; unused facts expire, popular ones live forever
- **Multi-project** — `NAMESPACE` isolates data between projects/agents; `tags` filter within one
- **Dual-scope access** — every tool call takes `shared: bool`, so one client can reach both its own namespaced area and the always-present shared area, per call
- **Shared deployment** — `REDIS_MEMORY_MCP_MODE=shared` lets many agents reuse one backend instead of each starting its own
- **Self-contained** — Docker stack: Redis Stack + HuggingFace TEI embeddings + MCP server

## Quick Start (standalone, outside Claude Code)

```bash
# 1. Clone (sparse checkout keeps just this package)
git clone --filter=blob:none --sparse https://github.com/sergesha/claude-essentials
cd claude-essentials && git sparse-checkout set redis-memory-mcp
cd redis-memory-mcp

# 2. Start infrastructure
docker compose up -d

# 3. Add to your AI tool's MCP config
```

### Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "redis-memory-mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "REDIS_URL=redis://host.docker.internal:6379/0",
        "-e", "EMBED_URL=http://host.docker.internal:8081",
        "-e", "INDEX_NAME=idx:memories",
        "redis-memory-mcp"
      ]
    }
  }
}
```

### Claude Code

```
/plugin marketplace add sergesha/claude-essentials
/plugin install redis-memory@claude-essentials
```

## Tools (8 total)

### Key-Value Storage — instant lookup

| Tool | Description |
|------|-------------|
| `kv_set(key, value, tags?, ttl_days?, shared?)` | Store a named fact |
| `kv_get(key, shared?)` | Retrieve by exact key (refreshes TTL) |
| `kv_delete(key, shared?)` | Delete by key |
| `kv_list(tag?, pattern?, shared?)` | List entries with filtering |

### Semantic Memory — vector search

| Tool | Description |
|------|-------------|
| `mem_save(text, code?, tags?, ttl_days?, shared?)` | Save fact with embedding |
| `mem_search(query, tags?, top_k?, shared?)` | Find by meaning (refreshes TTL on hits) |
| `mem_list(limit?, tag?, shared?)` | Browse by recency |
| `mem_delete(memory_id, shared?)` | Delete by ID |

`shared` (default `false`, all tools) — see [`shared` — reaching both areas from one client](#shared--reaching-both-areas-from-one-client) below.

## TTL & Auto-Expiry

| TTL | Use case |
|-----|----------|
| `ttl_days=90` (default) | Normal facts — expire if unused for 90 days |
| `ttl_days=0` | Permanent — API keys, critical config |
| `ttl_days=7` | Short-lived context |

- TTL **resets on every read** — frequently accessed facts never expire
- Redis `volatile-lru` evicts least-recently-used facts under memory pressure
- Only facts with TTL can be evicted; permanent facts (`ttl_days=0`) are safe

## Architecture

```
┌─────────────────┐     ┌────────────────────┐     ┌───────────────────┐
│  Cursor / Claude │────▶│  redis-memory-mcp  │────▶│   Redis Stack     │
│  (MCP client)    │ MCP │  (Python, stdio)   │     │   + RediSearch    │
└─────────────────┘     └────────┬───────────┘     │   + HNSW index    │
                                 │                  └───────────────────┘
                                 ▼
                        ┌────────────────────┐
                        │  HuggingFace TEI   │
                        │  (embeddings, CPU) │
                        └────────────────────┘
```

- **Redis Stack** — RediSearch module with HNSW vector index (768 dim, cosine)
- **TEI** — `paraphrase-multilingual-mpnet-base-v2` (multilingual, runs on CPU)
- **MCP server** — Python FastMCP over stdio

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `EMBED_URL` | `http://localhost:8081` | TEI embeddings endpoint |
| `INDEX_NAME` | `idx:memories` (or `idx:memories:{NAMESPACE}`, see below) | Redis search index name |
| `NAMESPACE` | unset | Isolates `kv_*`/`mem_*` data on a shared instance — see below |
| `DEFAULT_TTL` | `7776000` (90 days) | Default TTL in seconds |
| `REDIS_MEMORY_MCP_MODE` | `dedicated` | `start.sh` only — see Shared Deployment below |
| `REDIS_MEMORY_MCP_REF` | unset (tracks latest release) | `start.sh` only — pin to a specific `redis-memory-mcp-vX.Y.Z` tag, or `main` for dev |

### Shared Deployment (one backend, many agents)

By default (`start.sh`, mode `dedicated`) every invocation brings up its own Redis Stack + TEI containers — fine for a single user on a laptop, wasteful when several agents share one machine (a fleet of Claude Code agents, for example): each would spin up a duplicate, unused backend.

`REDIS_MEMORY_MCP_MODE=shared` skips that: it requires `REDIS_URL` and `EMBED_URL` to already point at a backend started elsewhere, and connects to it instead of starting a new one. Typical layout — one user/process owns the backend (plain `docker compose up -d redis embeddings redis-init`, no `start.sh` involved), every other user's MCP client config runs `start.sh` with:

```bash
REDIS_MEMORY_MCP_MODE=shared
REDIS_URL=redis://<backend-host>:6379/0
EMBED_URL=http://<backend-host>:8081
```

Sharing a backend does **not** mean sharing data — see `NAMESPACE` below for isolating agents that happen to point at the same Redis/TEI.

### NAMESPACE (data isolation on a shared instance)

`tags` (accepted by `kv_set`/`mem_save`, used as an optional filter by `mem_search`/`kv_list`/`mem_list`) are a same-namespace filter, not an isolation boundary: `kv_get`/`kv_set` operate on an exact key with no tag involved at all, so two agents on a shared instance calling `kv_set('database-url', ...)` would collide regardless of tags, and an untagged `mem_search` sees every agent's memories.

`NAMESPACE` fixes that at the key level. Set once per MCP client (e.g. `-e NAMESPACE=my-project`), it prefixes every key and picks a dedicated search index, so two namespaces cannot see or overwrite each other's data no matter what tags (or no tags) are passed:

```
NAMESPACE unset      → mem:{id}              kv:{key}              idx:memories            (previous behavior, unchanged)
NAMESPACE=my-project → ns:my-project:mem:{id} ns:my-project:kv:{key} idx:memories:my-project
```

(Namespaced keys are prefixed with `ns:{NAMESPACE}:`, not `mem:{NAMESPACE}:` — deliberately not a string extension of the base `mem:`/`kv:` prefixes. RediSearch's `FT.CREATE ... PREFIX` match is a plain string-prefix test, so a namespaced prefix that merely extends the base one would make the base index also pick up every namespace's keys once both exist. Keeping the two prefix sets disjoint avoids that.)

Combine with shared deployment as needed: same backend + no `NAMESPACE` = one shared memory across every agent; same backend + distinct `NAMESPACE` per agent = shared infrastructure, isolated data.

### `shared` — reaching both areas from one client

Every `kv_*`/`mem_*` tool also takes a `shared: bool = false` parameter, independent of `NAMESPACE`. This is a **per-call** choice, not a per-client one: a single MCP client running with `NAMESPACE=my-project` can write/read its own isolated area (`shared=false`, the default) *and* the always-present fleet-wide area (`shared=true`) — without a second registration, second backend, or second running process.

```
kv_set('db-url', '...')                    # own area (NAMESPACE's, or base if NAMESPACE unset)
kv_set('db-url', '...', shared=True)       # base/shared area, regardless of NAMESPACE
mem_search('deploy steps')                 # searches own area only
mem_search('deploy steps', shared=True)    # searches shared area only — never both, no merging
```

There's no automatic fallback between the two: a call touches exactly one area, and the caller decides which by setting `shared`. Reading something saved with `shared=True` requires `shared=True` on the read too, or it reports "not found" even though the entry exists in the other area.

## Redis UI

RedisInsight is included at **http://localhost:8001** — browse keys, run queries, analyze memory usage.

## Plugin Structure

```
redis-memory-mcp/                     # this package, within the claude-essentials marketplace
├── .claude-plugin/
│   ├── plugin.json                   # Plugin metadata
│   └── mcp.json                      # MCP server docs
├── .mcp.json                         # Runtime MCP config
├── hooks/project-init.json           # Session start hook
├── skills/persistent-memory/
│   └── SKILL.md                      # Memory management skill
├── server/                           # MCP server source
│   ├── memory_mcp.py
│   ├── Dockerfile
│   └── pyproject.toml
├── docker-compose.yaml               # Full stack (standalone use)
├── start.sh                          # Self-installer used by .mcp.json
└── README.md                         # this file
```

## License

MIT
