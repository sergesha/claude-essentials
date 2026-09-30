---
name: persistent-memory
description: >
  Use when starting tasks or looking up, saving, listing, or deleting
  persistent cross-session memory through redis-memory-mcp, including
  task context that supplies an isolated scope.

allowed_tools:
  - search
  - kv_set
  - kv_get
  - kv_delete
  - kv_list
  - mem_save
  - mem_search
  - mem_list
  - mem_delete
---

# Persistent Memory

Cross-session memory for AI agents using `redis-memory-mcp`.  
Requires MCP server `redis-memory-mcp` to be running.

## Choose the Memory Area

All nine tools accept `shared: bool = False` and `scope: str | None = None`.
`NAMESPACE` is fixed by the MCP connection. `shared` chooses the existing
own/shared area; `scope` selects an independent subspace inside that area.

| Connection / arguments | Selected area |
|---|---|
| Named `NAMESPACE`, arguments omitted | Existing own area, without a scope |
| Named `NAMESPACE`, `scope=S` | Subspace S inside the own area |
| Any connection, `shared=True` | Existing shared area, without a scope |
| Any connection, `shared=True, scope=S` | Subspace S inside the shared area |
| Empty `NAMESPACE`, `scope=S` | The same shared subspace S, with either shared value |

Each operation touches one area. **Without scope, search/list access only the
existing unscoped area; they do not discover named subspaces.** With scope,
operations do not fall back to the parent or other scopes. `search` combines
KV and semantic results only within the selected area.

Use the area supplied or authorized by the user/host. When task context supplies
a scope, pass its exact value and the selected `shared` value on **every** memory
call: task-start search, saves, reads, lists, deletes, and automatic reflections
or learning captures. A missing result is not permission to try another area.
Without an assigned scope, omit it to preserve existing behavior. Do not create
a new scope just to read existing data.

```python
# scope_id is supplied by the user/host; reuse the same area throughout.
area = {"shared": True, "scope": scope_id}
search(query="Earlier decisions", **area)
mem_save(text="The chosen approach and its rationale.", tags="project,design", **area)
kv_get(key="status", **area)
mem_delete(memory_id=memory_id, **area)
```

Scope names are case-sensitive, 1-128 ASCII letters, digits, `_` or `-`.
Empty strings, whitespace, separators and wildcards are invalid; preserve the
exact name rather than cleaning it. There is no tool to enumerate scopes.

An ordinary name organizes data. A secret scope with at least 128 bits of
cryptographic randomness acts as a bearer key for MCP-only access: knowing it
permits reading, writing and deletion. Have trusted code generate it (for
example, `secrets.token_urlsafe(32)`); do not invent a memorable name for
confidentiality. Use only authorized scopes. Keep secret names out of stored
labels/tags/text, unscoped memory and public output. They still appear in tool
arguments and Redis keys/indexes, so direct backend access or exposed logs can
reveal them. Scope isolation is not encryption or per-holder authorization.

## Tools Reference

The parameter tables below list tool-specific arguments; all tools also take
the common `shared` and `scope` arguments above. Workflow examples without
them use the original area. Apply the task's selected area to those examples.

### Key-Value Storage (`kv_*`) — instant O(1) lookup, **short discrete values only**

**Rule: kv is for values you retrieve by exact name.** If the value is longer than ~200 chars
or describes/explains something — use `mem_save` instead.

✅ Good kv: non-secret URL/host, version number, flag, short JSON config, timezone, username.
❌ Bad kv: architecture description, tech stack list, workflow explanation, pattern description.
❌ Never kv: API keys, passwords, tokens, or any secret — the default backend has no
   Redis password, and unrestricted direct Redis access bypasses scope isolation
   (see [Security](#security)). Store a *reference* to where the secret lives, not the secret.

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `kv_set` | `key` (str), `value` (str), `label` (str, optional), `tags` (str, optional), `ttl_days` (int, default 90) | Store named fact. `label` — short human-readable description. Overwrites if key exists. |
| `kv_get` | `key` (str) | Retrieve by exact key. Refreshes TTL on read. |
| `kv_delete` | `key` (str) | Delete by key. |
| `kv_list` | `tag` (str, optional), `pattern` (str, optional) | List entries. Filter by tag or glob pattern. |

### Semantic Memory (`mem_*`) — vector similarity search, **knowledge and descriptions**

**Rule: mem is for knowledge found by meaning.** Descriptions, patterns, decisions, lessons,
architecture notes, explanations — anything that answers "how", "why", "what happened".

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `mem_save` | `text` (str), `label` (str, optional), `code` (str, optional), `tags` (str, optional), `ttl_days` (int, default 90) | Save with embedding. `label` — short human-readable description. Found by meaning. |
| `mem_search` | `query` (str), `tags` (str, optional), `top_k` (int, default 5) | Search by meaning. Refreshes TTL on hits. |
| `mem_list` | `limit` (int, default 20), `tag` (str, optional) | Browse by recency. |
| `mem_delete` | `memory_id` (str) | Delete by UUID from search results. |

### Unified Search (`search`) — both stores in the selected area

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `search` | `query` (str), `tags` (str, optional), `top_k` (int, default 5) | **Default search tool.** Searches both kv (by substring) and mem (by meaning). Use this when you don't know where the fact is stored. |

> `search` is a convenience wrapper. Individual tools (`kv_get`, `kv_list`, `mem_search`) remain available
> for targeted access when you already know the store.

### TTL & Auto-Expiry

- **Default: 90 days** — unused facts auto-expire
- **TTL resets on read** — popular facts live forever
- **`ttl_days=0`** — permanent, never expires. Use only in extreme cases where loss is truly unacceptable.
- **`ttl_days=7`** or `30` — short-lived context
- **volatile-lru** — Redis evicts least-recently-used facts with TTL under memory pressure

### When to Use Which

| Need | Tool | Example |
|------|------|---------|
| Exact short value by name | `kv_set` / `kv_get` | `kv_set('prod-db-url', 'postgresql://host:5432/db', label='Production DB URL', tags='db,prod')` |
| Search everything at once | `search` | `search(query='database connection', tags='project')` |
| Find knowledge by meaning | `mem_save` / `mem_search` | `mem_save(text='JWT with 24h expiry, refresh in Redis', label='JWT auth strategy', tags='auth,jwt')` |
| Non-secret config value | `kv_set` | `kv_set('prod-db-host', 'db.internal:5432', label='Production DB host', tags='db')` |
| Architecture / patterns | `mem_save` | `mem_save(text='DDD with layered structure...', label='Project architecture', tags='project,architecture')` |
| Lessons learned | `mem_save` | `mem_save(text='Problem: X. Solution: Y.', label='Lesson: X solved', tags='project,lessons')` |
| Bug fix with code | `mem_save` | `mem_save(text='Race condition in auth', label='Auth race condition fix', code='async def ...', tags='bugs')` |

## Process Triggers

### 🔍 SEARCH memory (before acting)

**1. Task Start (MANDATORY)**
Before ANY new task — search for similar past work:
```
search(query="[task description]", tags="[project]", top_k=5)
```
Present findings before starting work.

**2. Problem Encountered**
When hitting a problem/error:
```
search(query="[error or problem description]", tags="[project]")
```

**3. Architecture/Design Decision**
Before making design choices — check past decisions:
```
search(query="[decision topic]", tags="[project],architecture")
```

**4. Configuration Lookup**
When needing a known value:
```
kv_get(key="[project]-db-url")
```

### 💾 SAVE to memory (after learning)

**5. Solution Found**
After solving a non-trivial problem:
```
mem_save(
  text="Problem: [X]. Solution: [Y]. Key insight: [Z]. Future: [when to reuse].",
  label="[Short description of what was solved]",
  tags="[project],[technology],[type]"
)
```

**6. Task Completed (>30 min or complex)**
Reflection: What worked? What didn't? What patterns emerged?
```
mem_save(
  text="Task: [X]. Approach: [Y]. Lesson: [Z]. Would do differently: [W].",
  label="[Task name] — lessons learned",
  tags="[project],lessons"
)
```

**7. Bug Fixed**
```
mem_save(
  text="Bug: [desc]. Root cause: [X]. Fix: [Y]. Prevention: [Z].",
  label="Bug: [short description]",
  code="[relevant code snippet]",
  tags="[project],bug-fix,[technology]"
)
```

**8. Architecture Decision**
```
mem_save(
  text="Decision: [X]. Rationale: [Y]. Alternatives: [Z]. Context: [W].",
  label="Architecture decision: [topic]",
  tags="[project],architecture,[domain]"
)
```

**9. Non-secret config**
```
kv_set(key="[project]-db-host", value="db.internal:5432", label="[Project] DB host", tags="[project],db,prod")
```
(Never store the password/DSN itself — see [Security](#security). Keep secrets in a secret
manager; store only a non-secret reference here.)

**10. Pattern Recognized**
```
mem_save(
  text="Pattern: [name]. When: [context]. How: [approach]. Why: [benefits].",
  label="Pattern: [name]",
  code="[example code]",
  tags="[project],pattern,[technology]"
)
```

## Project Tags

**Always include project tag** as first tag in every save and search:
```
tags="myproject,auth,backend"    ← project is first
```

Tags organize/filter entries within the selected area; they do not isolate
projects or scope KV keys. Multiple search tags match ANY supplied tag (OR),
so `tags="myproject,auth"` can also match another project's `auth` entries.
Use `NAMESPACE` and/or an assigned scope when separate areas are required.

## Priority Rules

1. **ALWAYS search before starting** — leverage past work
2. **ALWAYS save solutions** — non-trivial problem → save pattern
3. **ALWAYS reflect after long tasks** — extract lessons
4. **ALWAYS tag with project** — consistent organization within the selected area
5. **ALWAYS use kv_* for named facts** — faster, more reliable than search
6. **`ttl_days=0` only in extreme cases** — permanent storage, no expiry ever. Almost never needed — TTL auto-resets on every read.

## Security

- **Not a secrets store.** By default the backend has no Redis password.
  Any client with unrestricted direct Redis access can read stored data, including
  scoped data. **Never** store API keys, passwords, tokens, or other secrets — keep those in a real
  secret manager or environment variables, and store at most a non-secret *reference* here.
- **`NAMESPACE` and `shared` are cooperative by default.** A client's startup configuration
  chooses its namespace; `shared=True` selects the commons. Optional Redis ACLs can
  enforce namespace boundaries (see README). Without ACLs, direct Redis access
  bypasses both namespace and secret-scope isolation.
- **Network exposure.** The shipped `docker-compose.yaml` publishes Redis on all host
  interfaces with no auth (and Docker's iptables rules typically bypass ufw). Run it only on
  a trusted network / behind a firewall. See README's Security section.

## Error Handling

If redis-memory-mcp is unavailable:
- Log warning, continue without memory
- Don't block the main workflow
- Inform user that memory features are temporarily disabled
