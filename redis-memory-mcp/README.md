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
- **Isolated subspaces** — optional per-call `scope` selects an independent subspace inside the own or shared area, without changing the MCP connection's `NAMESPACE`
- **Shared deployment** — `REDIS_MEMORY_MCP_MODE=shared` lets many agents reuse one backend instead of each starting its own
- **Self-contained** — Docker stack: Redis Stack + EmbeddingGemma Q4 embeddings + MCP server

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
        "-e", "EMBED_URL=http://host.docker.internal:8082",
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
  --config embed_url=http://127.0.0.1:8082 \
  --config namespace=my-project   # omit for the fleet-wide/shared default
```

### Codex

Install the complete plugin from the repository marketplace (not a standalone
copy of the skill):

```bash
codex plugin marketplace add sergesha/claude-essentials
codex plugin add redis-memory@claude-essentials
```

Codex uses the same `start.sh`, server, memory skill and SessionStart notice as
Claude Code. Docker, Bash and curl must be available on the host's `PATH`.
The default is the existing dedicated backend mode. To reuse a running backend,
set these variables in the environment of the **Codex process**, then start a
new Codex session:

```bash
export REDIS_MEMORY_MCP_MODE=shared
export REDIS_URL=redis://host.docker.internal:6379/0
export EMBED_URL=http://host.docker.internal:8082
export NAMESPACE=my-project
codex
```

The first start may download and build the bridge image. Codex allows up to five
minutes for that startup; memory remains optional if the backend is unavailable.

Keep the same namespace (or leave it unset for the existing shared/base area)
to access existing memories. The URLs must be reachable from the bridge
container: `localhost` there is not the host. For other layouts use the existing
network/socket settings documented below.

Codex forwards `REDIS_MEMORY_MCP_MODE`, `REDIS_URL`, `EMBED_URL`, `NAMESPACE`,
`INDEX_NAME`, `REDIS_MEMORY_MCP_NETWORK`, `EMBED_SOCKET`,
`REDIS_MEMORY_MCP_SOCKET_DIR` and `REDIS_MEMORY_MCP_REF` to the launcher, together
with `DOCKER_HOST`/`DOCKER_CONTEXT` when set. It does not use Claude's
`userConfig` placeholders or `claude plugin install --config` options.
For the desktop app, make those variables available to the app's process;
exporting them in an unrelated terminal after the app has started has no effect.

For an authenticated Redis URL, obtain the value from your secret manager into
the process environment. Do not paste a password into shell history, plugin
manifests, command arguments, issue reports or logs. Redis ACLs, namespace
prefixes and embedding configuration are unchanged by this integration.

### Upgrade (both clients)

Follow the shared [plugin update procedure](../docs/plugin-updates.md) for
installation, session activation, and verification in both clients.

Upgrading the plugin does not migrate,
clear or rename stored memories. Keep the existing backend credentials,
namespace, index and embeddings settings. The existing launcher selects the
latest `redis-memory-mcp-v*` release unless `REDIS_MEMORY_MCP_REF` is pinned;
a pinned backend remains pinned independently of the plugin version.

## Tools

### Key-Value Storage — instant lookup

| Tool | Description |
|------|-------------|
| `kv_set(key, value, tags?, ttl_days?, shared?, scope?)` | Store a named fact |
| `kv_get(key, shared?, scope?)` | Retrieve by exact key (refreshes TTL) |
| `kv_delete(key, shared?, scope?)` | Delete by key |
| `kv_list(tag?, pattern?, shared?, scope?)` | List entries with filtering |

### Semantic Memory — vector search

| Tool | Description |
|------|-------------|
| `mem_save(text, code?, tags?, ttl_days?, shared?, scope?)` | Save fact with embedding |
| `mem_search(query, tags?, top_k?, shared?, scope?)` | Find by meaning (refreshes TTL on hits) |
| `mem_list(limit?, tag?, shared?, scope?)` | Browse by recency |
| `mem_delete(memory_id, shared?, scope?)` | Delete by ID |
| `search(query, tags?, top_k?, shared?, scope?)` | Search KV and semantic memory within one selected area |

`shared` (default `false`, all tools) — see [`shared` — reaching both areas from one client](#shared--reaching-both-areas-from-one-client) below.
`scope` (default `None`, all tools) — see [isolated subspaces](#scope--isolated-subspaces) below.

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
└─────────────────┘     └────────┬───────────┘     │   + FLAT index    │
                                 │                  └───────────────────┘
                                 ▼
                        ┌────────────────────┐
                        │  EmbeddingGemma Q4   │
                        │  (embeddings, CPU) │
                        └────────────────────┘
```

- **Redis Stack** — RediSearch module with exact FLAT vector indexes (256 dimensions by default, cosine)
- **Gemma service** — `onnx-community/embeddinggemma-300m-ONNX`, pinned Q4 conversion (multilingual, ONNX Runtime CPU)
- **MCP server** — Python MCP SDK 2.x (`MCPServer`) over stdio

The server uses the [current SDK API](https://py.sdk.modelcontextprotocol.io/migration/#fastmcp-renamed-to-mcpserver),
not the removed `mcp.server.fastmcp` import. Existing tool names, parameters and
canonical memory payloads are preserved. Existing semantic vectors require the explicit [migration](MIGRATION.md).

## Native Python

The MCP bridge can run directly with Python 3.11 or newer, without Docker or
Podman in the server process. Install the tagged server source into a virtual
environment (replace the tag after later releases):

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install \
  'https://github.com/sergesha/claude-essentials/archive/refs/tags/redis-memory-mcp-v0.11.0.tar.gz#subdirectory=redis-memory-mcp/server'
```

Point the executable at backend services that are already reachable, then run
it as a stdio MCP server:

```bash
export REDIS_URL=redis://127.0.0.1:6379/0
export EMBED_URL=http://127.0.0.1:8082
export NAMESPACE=my-project  # optional
.venv/bin/redis-memory-mcp
```

Semantic tools require both Redis Stack and the embeddings service; the KV
tools require Redis only. Put optional Redis ACL credentials in `REDIS_URL` and
keep them out of command arguments and committed MCP configuration. Native
execution preserves the existing namespace and ACL behavior. Launcher-only
settings such as `REDIS_MEMORY_MCP_MODE`, `REDIS_MEMORY_MCP_NETWORK`, and
`REDIS_MEMORY_MCP_REF` do not provision anything in the native process.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `EMBED_URL` | `http://localhost:8082` | Compatible Gemma Q4 HTTP endpoint |
| `INDEX_NAME` | `idx:memories` (or `idx:memories:{NAMESPACE}`, see below) | Redis search index name |
| `NAMESPACE` | unset | Isolates `kv_*`/`mem_*` data on a shared instance — see below |
| `DEFAULT_TTL` | `7776000` (90 days) | Default TTL in seconds |
| `REDIS_MEMORY_MCP_MODE` | `dedicated` | `start.sh` only — see Shared Deployment below |
| `REDIS_MEMORY_MCP_REF` | unset (tracks latest release) | `start.sh` only — pin to a specific `redis-memory-mcp-vX.Y.Z` tag, or `main` for dev |
| `REDIS_MEMORY_MCP_NETWORK` | unset | Join an existing Docker/Podman network to reach the backend by container name; does not create a network |
| `EMBED_SOCKET` | unset | Unix socket for embedding HTTP requests; `EMBED_URL` remains the nominal HTTP URL |
| `REDIS_MEMORY_MCP_SOCKET_DIR` | unset | Bind-mount this host socket directory at the same path in the bridge; uses Podman's `--group-add keep-groups` for group-gated sockets |

### Shared Deployment (one backend, many agents)

By default (`start.sh`, mode `dedicated`) the launcher ensures the shared local Redis Stack and Gemma containers are running. Each stdio MCP connection still has its own lightweight bridge container; the backend and model are shared.

`REDIS_MEMORY_MCP_MODE=shared` skips that: it requires `REDIS_URL` and `EMBED_URL` to already point at a backend started elsewhere, and connects to it instead of starting a new one. Typical layout — one user/process owns the backend (plain `docker compose up -d redis embeddings`, no `start.sh` involved), every other user's MCP client config runs `start.sh` with:

```bash
REDIS_MEMORY_MCP_MODE=shared
REDIS_URL=redis://<backend-host>:6379/0
EMBED_URL=http://<backend-host>:8082
```

Sharing a backend need not mean sharing data — see `NAMESPACE` below for keeping agents that point at the same Redis/Gemma in separate key areas. Note this is a cooperative convention, not an enforced boundary (see [Security](#security)).

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

### `scope` — isolated subspaces

Every tool accepts optional `scope`. First the existing `NAMESPACE`/`shared`
rules choose an area, then `scope` selects a separate subspace within it.
The MCP connection's `NAMESPACE` never changes. With no scope (or `None`), all
existing keys, indexes, results and TTL behavior remain unchanged.

| MCP configuration / call | Selected area |
|---|---|
| `NAMESPACE=alpha`, no scope | Existing alpha area |
| `NAMESPACE=alpha`, `scope=S` | Alpha's subspace S |
| Any namespace, `shared=True`, no scope | Existing shared area |
| Any namespace, `shared=True, scope=S` | Shared subspace S |
| Empty namespace, `scope=S` | The same shared subspace S, regardless of `shared` |

```python
# An ordinary name separates data; it is not a secret access key.
kv_set("status", "active", scope="task-a")
kv_get("status", scope="task-a")
mem_save(text="A decision for the shared subspace.", shared=True, scope="task-a")
search(query="decision", shared=True, scope="task-a")
```

Use the same `shared` and `scope` on save, read, search, list and delete.
Unscoped searches/lists exclude all named subspaces; scoped operations exclude
the parent and siblings. There is no recursive/all-scopes search, scope listing,
fallback, or global scope switch. Parallel calls can select different subspaces.
Tags remain filters within an area; multiple search tags use OR.

Names are case-sensitive, 1-128 ASCII letters, digits, underscores or hyphens.
Empty strings, whitespace, separators and glob characters are rejected before
Redis/embedding access. Names are never trimmed, sanitized or normalized.

```
alpha, scope=S  → ns:alpha:scope:S:mem:{id}  ns:alpha:scope:S:kv:{key}  idx:memories:scope:alpha:S
shared, scope=S → ns::scope:S:mem:{id}       ns::scope:S:kv:{key}       idx:memories:scope::S
```

These prefixes are disjoint from all existing parent prefixes. Subspace indexes
are created on the first semantic save; reads of an absent index return empty
without creating one. Existing subspace index definitions and search-result
prefixes are checked before data is returned. Key TTLs still refresh on the
same reads/search hits as before. Indexes do not expire with records, so operators
must clean up unused indexes separately; scoped writes are not quota-limited.

For **MCP-only access**, a secret name with at least 128 bits of cryptographic
randomness can serve as a bearer capability. Trusted code can generate it with
`secrets.token_urlsafe(32)`. Anyone knowing it can read, write and delete that
subspace; there are no per-holder permissions or individual revocation. Do not
publish secret scope names in memory content, labels, tags or public output.
They occur in tool arguments, Redis keys and index names: backend access and
exposed logs bypass this protection. This is not encryption or a secrets store.

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
- **Ports publish on all interfaces.** `6379` (Redis) and `8082` (Gemma service embeddings) are published
  without a host-IP restriction, so Docker binds them on every interface — and Docker's iptables
  rules typically **bypass `ufw`**. On a host with a public IP that is an open, unauthenticated
  database. Restrict it: bind the ports to a trusted interface, put the host behind a firewall
  Docker cannot bypass, or run the whole stack on an isolated network.
- **No RedisInsight GUI.** This package uses `redis/redis-stack-server` (server only); it does
  not expose the RedisInsight web UI (port 8001).

### Enforced multi-tenant isolation (Redis ACLs)

For a shared host with mutually-distrusting tenants, turn `NAMESPACE` from a cooperative
convention into an **enforced** boundary: each namespace authenticates as its own Redis user,
scoped by key pattern, so one tenant cannot read or write another's keys even over a direct
Redis connection. This is **opt-in** — the base `docker-compose.yaml` is unchanged, so the
single-user quick-start keeps working with no auth.

1. Copy the template (`redis-acl.example.acl` — see that file for the exact lines) to the real,
   gitignored file. Every user except `default` ships **disabled** (`off`) with a `CHANGE_ME_`
   placeholder password, so an unedited copy is **fail-safe** — the backend loads but rejects
   every login. To enable a user, flip `off` → `on` and replace its `CHANGE_ME_` with a strong,
   unique password. Add one `user ns_<name>` line per `NAMESPACE` you run.

   ```bash
   cp redis-acl.example.acl redis-acl.acl   # gitignored; this file IS a secret
   # edit redis-acl.acl: enable (on) + set a real password for each user you use
   ```

   The users in the template:

   - `default` — locked: data access denied, only the unauthenticated `ping` healthcheck allowed.
   - `admin` — manual ops / provisioning only (never used by agents). Strong password.
   - `shared` — clients that run with **no** `NAMESPACE`: the shared/base commons only.
   - `ns_<name>` — one per namespace: its private area (`ns:<name>:*` + index `idx:memories:<name>`)
     and its scoped indexes (`idx:memories:scope:<name>:*`), **plus** the shared commons
     (`~mem:* ~kv:* ~idx:memories`) and shared subspaces
     (`~ns::scope:* ~idx:memories:scope::*` — what `shared=True, scope=...` reaches).
     Drop all shared-area grants from a line to wall that tenant off from the commons.
     `-@dangerous -@admin` block `FLUSHALL`/`KEYS`/`CONFIG`/`ACL`; the server needs only hashes,
     `SCAN`, `EXPIRE`, `DEL`, and `FT.*`, all still permitted.

   Existing ACL files must explicitly grant scoped prefixes/indexes before scoped
   calls can succeed. The updated template keeps each user's existing own/shared
   access and adds only the corresponding subspaces; it does not enumerate or
   authorize scopes individually. Secret-scope confidentiality assumes agents
   cannot use these broad Redis credentials directly.

   > **Redis ACL files allow no comments or blank lines** — every line must start with `user`.
   > Keep `redis-acl.acl` to `user …` lines only. Passwords written as `>plaintext` are hashed
   > by Redis on load.

2. Start with the ACL overlay on top of the base compose file:

   ```bash
   docker compose -f docker-compose.yaml -f docker-compose.acl.yaml up -d
   ```

   The overlay mounts `redis-acl.acl` as a Compose **config**, so if you forget step 1 the command
   fails immediately (missing source path) instead of silently creating a directory there.

3. Point each client's `REDIS_URL` at its own namespace user (the credentials ride in the URL —
   no server-side change needed):

   ```bash
   REDIS_URL=redis://ns_alice:<password>@host.docker.internal:6379/0
   NAMESPACE=alice
   ```

   Set it as an **environment variable**, never as a literal in the MCP entry's `args` (the
   Cursor example above shows the unauthenticated default, and an authed URL does not belong
   there). A value in `args` becomes part of the process command line, and `/proc/<pid>/cmdline`
   is world-readable: every other local user, and anything that prints a process tree, reads the
   password in clear text. `start.sh` passes all five variables to the container by name for the
   same reason.

Enforcement is real: a direct `redis-cli` as one namespace user gets `NOPERM` on another
namespace's keys, and `FLUSHALL`/`CONFIG` are denied to every namespace user.

This is the follow-up tracked as [#17](https://github.com/sergesha/claude-essentials/issues/17).
Without the overlay, treat the backend as a shared, unauthenticated cache among mutually
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

## Gemma Q4 and configurable chunks

Semantic memory now uses one shared EmbeddingGemma Q4 CPU service, paragraph
chunks and exact Redis FLAT retrieval. A long record keeps one canonical ID;
search returns complete unique records ranked by their best chunk. Returned hits
refresh parent and existing chunk TTLs together. KV and scope selection are unchanged.

| Startup setting | Default | Supported values |
|---|---:|---|
| `CHUNK_MAX_TOKENS` |256|64–2048 total tokens including label/prompt/specials|
| `EMBED_DIMENSION` |256|128,256,512,768|

Set these in the MCP process environment, pass them through the launcher, or use
the Claude plugin `chunk_max_tokens`/`embed_dimension` settings. Codex forwards the
environment variables. Native installs use the same names. Docker Compose accepts
these variables or a `.env` file. **Changing either requires the explicit migration
below for existing semantic data; restarting alone cannot convert old vectors.**

```bash
CHUNK_MAX_TOKENS=256 EMBED_DIMENSION=256 bash redis-memory-mcp/start.sh
# Native shared embedding service (once per backend):
pip install './redis-memory-mcp/server[embeddings]'
EMBED_PORT=8082 redis-memory-embeddings
# Native MCP process (one per client):
EMBED_URL=http://127.0.0.1:8082 CHUNK_MAX_TOKENS=256 EMBED_DIMENSION=256 redis-memory-mcp
```

The native embedding command binds 127.0.0.1:8081 by default; set `EMBED_PORT=8082`
for the new dedicated-stack port. Docker dedicated mode uses8082, a new
`embeddings-gemma` container and `gemma_cache`, preserving the previous 8081 endpoint
for migration/rollback. It does not automatically migrate records or remove old
containers. `REDIS_MEMORY_MCP_MODE=shared` still requires explicit `REDIS_URL` and
`EMBED_URL`; the endpoint must advertise the pinned Gemma profile, not an arbitrary embedding service.

The service uses the pinned ONNX-community Gemma conversion, verifies artifact
hashes, and loads model weights once. MCP clients load only the matching tokenizer.
No input is silently truncated. Label context is capped at min(64,chunk-budget/4)
tokens while the stored full label is preserved. Overlap32 is applied only when
splitting oversized paragraphs; it is not an overlap between every adjacent chunk.
A record requiring more than256 chunks is rejected before publication. Queries
must fit2048 tokens including their prompt. Embedding dimension truncation is
followed by normalization. Returned similarity is a ranking score, not confidence.

**Existing installations:** follow [MIGRATION.md](MIGRATION.md) for backup, offline
build/resume, verification, coordinated cutover and separate cleanup. Existing ACL
users need derived-key and index grants from the updated example. Admin migration
permissions are not granted to ordinary namespaced MCP clients.

### Model licensing

Plugin code remains MIT. EmbeddingGemma weights/Q4 derivatives have separate
[model-use terms](server/licenses/MODEL-USE-TERMS.md), including the complete
[Gemma Terms](server/licenses/GEMMA-TERMS.txt) and
[Prohibited Use Policy](server/licenses/GEMMA-PROHIBITED-USE.txt).
Read them before the first model download/use. Redistributors and hosted-service
operators must pass on the model restrictions and terms; an MIT notice alone is
insufficient. [Notice](server/licenses/NOTICE) and
[provenance/modifications](server/licenses/THIRD-PARTY.md) ship in the wheel and images.
