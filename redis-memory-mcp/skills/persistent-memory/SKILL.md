---
name: persistent-memory
description: >
  Manages persistent cross-session memory using redis-memory-mcp.
  Two modes: semantic search (mem_*) for knowledge by meaning,
  key-value (kv_*) for instant lookup. Auto-expiry via TTL + LRU.
  Triggers: task start (search), solution found (save), bug fix (save),
  architecture decision (save), task complete (reflection + save).

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

## Tools Reference

### Key-Value Storage (`kv_*`) — instant O(1) lookup, **short discrete values only**

**Rule: kv is for values you retrieve by exact name.** If the value is longer than ~200 chars
or describes/explains something — use `mem_save` instead.

✅ Good kv: URL, API key, version number, flag, short JSON config, timezone, username.
❌ Bad kv: architecture description, tech stack list, workflow explanation, pattern description.

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

### Unified Search (`search`) — search everywhere at once

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
| Config value / credential | `kv_set` | `kv_set('openai-key', 'sk-...', label='OpenAI API key', tags='secrets')` |
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

**9. Config/Credentials**
```
kv_set(key="[project]-db-url", value="postgresql://...", label="[Project] DB URL", tags="[project],db,prod")
```

**10. Pattern Recognized**
```
mem_save(
  text="Pattern: [name]. When: [context]. How: [approach]. Why: [benefits].",
  label="Pattern: [name]",
  code="[example code]",
  tags="[project],pattern,[technology]"
)
```

## Multi-Project Isolation

**Always include project tag** as first tag in every save and search:
```
tags="myproject,auth,backend"    ← project is first
```

This ensures facts don't mix between projects. Use consistent project identifiers.

## Priority Rules

1. **ALWAYS search before starting** — leverage past work
2. **ALWAYS save solutions** — non-trivial problem → save pattern
3. **ALWAYS reflect after long tasks** — extract lessons
4. **ALWAYS tag with project** — multi-project isolation
5. **ALWAYS use kv_* for named facts** — faster, more reliable than search
6. **`ttl_days=0` only in extreme cases** — permanent storage, no expiry ever. Almost never needed — TTL auto-resets on every read.

## Error Handling

If redis-memory-mcp is unavailable:
- Log warning, continue without memory
- Don't block the main workflow
- Inform user that memory features are temporarily disabled
