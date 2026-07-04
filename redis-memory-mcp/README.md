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

Installing this way prompts for `mode`/`redis_url`/`embed_url`/`namespace` interactively — these
are the plugin's `userConfig` fields, the only way its bundled `.mcp.json` receives them (plugin
MCP servers do **not** inherit the installing shell's environment, e.g. `.bashrc` exports; only
values explicitly declared via `userConfig` and referenced as `${user_config.KEY}` reach the
spawned process). For a non-interactive install (e.g. scripted, over SSH), pass them directly:

```bash
claude plugin install redis-memory@claude-essentials \
  --config mode=shared \
  --config redis_url=redis://127.0.0.1:6379/0 \
  --config embed_url=http://127.0.0.1:8081 \
  --config namespace=my-project   # omit for the fleet-wide/shared default
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
| `ttl_days=0` | Permanent — stable non-secret config (never secrets, see [Security](#security)) |
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

Sharing a backend need not mean sharing data — see `NAMESPACE` below for keeping agents that point at the same Redis/TEI in separate key areas. Note this is a cooperative convention, not an enforced boundary (see [Security](#security)).

### NAMESPACE (data isolation on a shared instance)

`tags` (accepted by `kv_set`/`mem_save`, used as an optional filter by `mem_search`/`kv_list`/`mem_list`) are a same-namespace filter, not an isolation boundary: `kv_get`/`kv_set` operate on an exact key with no tag involved at all, so two agents on a shared instance calling `kv_set('database-url', ...)` would collide regardless of tags, and an untagged `mem_search` sees every agent's memories.

`NAMESPACE` fixes that at the key level. Set once per MCP client (e.g. `-e NAMESPACE=my-project`), it prefixes every key and picks a dedicated search index, so two well-behaved namespaced clients won't see or overwrite each other's data through these tools, no matter what tags (or no tags) are passed. This is collision-avoidance between cooperating clients, **not** an enforced security boundary — a client can pick any namespace or pass `shared=True`, and a direct Redis connection reads everything (see [Security](#security)):

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

## Security

**This is not a secrets store, and its default deployment is not hardened.** Read this before
storing anything sensitive or exposing the backend beyond a single trusted machine.

- **No per-client auth, no password by default.** The shipped `docker-compose.yaml` runs Redis
  with no `requirepass`. Anything stored is readable by every client that can reach the Redis
  port. **Never store API keys, passwords, tokens, or other secrets** — keep those in a real
  secret manager or environment variables and store at most a non-secret *reference* here.
- **`NAMESPACE` and `shared` are cooperative, not a security boundary.** They are a key-prefix
  convention with no server-side enforcement: any client can choose any `NAMESPACE` or pass
  `shared=True`, and anyone with direct Redis access reads every namespace's keys regardless.
  Two namespaces are isolated *only* for well-behaved clients going through these tools — not
  against a direct connection or a client that simply picks another namespace's prefix.
- **Ports publish on all interfaces.** `6379` (Redis) and `8081` (TEI embeddings) are published
  without a host-IP restriction, so Docker binds them on every interface — and Docker's iptables
  rules typically **bypass `ufw`**. On a host with a public IP that is an open, unauthenticated
  database. Restrict it: bind the ports to a trusted interface, put the host behind a firewall
  Docker cannot bypass, or run the whole stack on an isolated network.
- **No RedisInsight GUI.** This package uses `redis/redis-stack-server` (server only); it does
  not expose the RedisInsight web UI (port 8001).

Enforced per-client authentication and per-`NAMESPACE` isolation (Redis ACLs) is tracked
separately — until then, treat the backend as a shared, unauthenticated cache among mutually
trusting clients.

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
