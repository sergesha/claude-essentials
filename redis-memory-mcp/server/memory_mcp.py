"""
Redis Memory MCP — Server
Two tool sets:
  kv_*  — simple key/value store (instant, no embeddings)
  mem_* — semantic memory (vector search via TEI + Redis HNSW)

TTL strategy (volatile-lru):
  - Every key has a TTL (default 90 days)
  - TTL is refreshed on every read → popular facts never expire
  - Unused facts expire after TTL → Redis evicts them under memory pressure
"""

import re, struct, time, uuid, os
from datetime import datetime, timezone

import httpx
import redis.asyncio as aio_redis
from mcp.server.fastmcp import FastMCP

REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
EMBED_URL    = os.getenv("EMBED_URL",    "http://localhost:8081")
# When set, embedding requests go over this Unix socket (e.g. a socat sidecar
# proxying TEI's HTTP port) instead of resolving EMBED_URL's host/port --
# httpx has no unix:// URL scheme, so this needs a real transport, not just an
# env value. EMBED_URL is still used as the request's nominal URL (path only
# matters; the transport intercepts the actual connection).
EMBED_SOCKET = os.getenv("EMBED_SOCKET", "")
TOP_K        = int(os.getenv("TOP_K",   "5"))
DEFAULT_TTL  = int(os.getenv("DEFAULT_TTL", str(90 * 24 * 3600)))  # 90 days

# NAMESPACE isolates data on a shared Redis instance — e.g. several agents
# pointed at the same REDIS_URL/EMBED_URL (one Redis Stack + one TEI service)
# but each with its own NAMESPACE never see or overwrite each other's keys,
# regardless of whether they pass `tags`. Tags remain a same-namespace
# filter; NAMESPACE is the isolation boundary. Unset = previous global
# behavior (single shared "mem:"/"kv:" keyspace, "idx:memories" index).
NAMESPACE    = re.sub(r"[^a-zA-Z0-9_-]", "", os.getenv("NAMESPACE", "").strip())
_BASE_INDEX  = os.getenv("INDEX_NAME", "idx:memories")
_BASE_MEM, _BASE_KV = "mem:", "kv:"
# Namespaced keys use "ns:{NAMESPACE}:mem:"/"...:kv:", NOT "mem:{NAMESPACE}:" —
# deliberately not a string extension of _BASE_MEM/_BASE_KV. RediSearch's
# FT.CREATE PREFIX match is a plain string-prefix test with no separator
# awareness: "mem:agentA:x" *does* start with "mem:", so an index created
# with PREFIX "mem:" would also pick up every namespace's keys once both the
# base and a namespaced index exist side by side (confirmed live — the base
# scope's mem_search returned another namespace's private entries). Prefixing
# with "ns:{NAMESPACE}:" instead makes the two prefix sets disjoint in both
# directions, so a key can only ever match one index.
_OWN_MEM = f"ns:{NAMESPACE}:mem:" if NAMESPACE else _BASE_MEM
_OWN_KV  = f"ns:{NAMESPACE}:kv:"  if NAMESPACE else _BASE_KV
_OWN_INDEX = f"{_BASE_INDEX}:{NAMESPACE}" if NAMESPACE else _BASE_INDEX

def _scope(shared: bool) -> tuple[str, str, str]:
    """Resolve (mem_prefix, kv_prefix, index) for one call.

    Every kv_*/mem_* tool takes `shared: bool = False` and calls this to pick
    which of two always-available areas that one call reads/writes:
      shared=False (default) → NAMESPACE's own area (or the base area if
        NAMESPACE is unset — same thing, so this is a no-op when there's no
        namespace to isolate from).
      shared=True  → the base ("mem:"/"kv:"/idx:memories) area, always,
        regardless of NAMESPACE.
    This is a per-call choice, not a per-server-instance one: one MCP client
    with NAMESPACE=myproject can still reach the fleet-wide shared area by
    passing shared=True on any individual call, without a second client/
    registration. There is no cross-scope fallback — a call touches exactly
    one area; the caller decides which by setting `shared`.
    """
    if shared or not NAMESPACE:
        return _BASE_MEM, _BASE_KV, _BASE_INDEX
    return _OWN_MEM, _OWN_KV, _OWN_INDEX

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_PREFIX_RE = re.compile(r"^[0-9a-f]{1,8}$")
_MAX_PREFIX_MATCHES = 10
_MAX_SCAN_ROUNDS = 3

mcp = FastMCP("Redis Memory")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encode(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

def _embed_client() -> httpx.AsyncClient:
    if EMBED_SOCKET:
        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=EMBED_SOCKET), timeout=30.0)
    return httpx.AsyncClient(timeout=30.0)

async def _embed(text: str) -> list[float]:
    async with _embed_client() as c:
        r = await c.post(f"{EMBED_URL}/embed", json={"inputs": text})
        r.raise_for_status()
        return r.json()[0]

def _redis():
    return aio_redis.from_url(REDIS_URL, decode_responses=False)

async def _ensure_index(r, index: str, mem_prefix: str):
    try:
        await r.execute_command("FT.INFO", index)
    except Exception:
        await r.execute_command(
            "FT.CREATE", index, "ON", "HASH", "PREFIX", "1", mem_prefix, "SCHEMA",
            "text",      "TEXT",
            "label",     "TEXT",
            "code",      "TEXT",
            "tags",      "TAG",    "SEPARATOR", ",",
            "vector",    "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "768", "DISTANCE_METRIC", "COSINE",
            "timestamp", "NUMERIC",
        )

def _decode(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)

def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"

def _fmt_ttl(seconds: int) -> str:
    if seconds < 0:
        return "no TTL"
    days = seconds // 86400
    if days > 0:
        return f"{days}d"
    hours = seconds // 3600
    return f"{hours}h"

def _sanitize_tag(tag: str) -> str:
    """Strip tag to safe chars only (letters, digits, hyphens, underscores)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", tag.strip())
    return cleaned

def _escape_tag(tag: str) -> str:
    """Backslash-escape RediSearch TAG special chars (e.g. hyphen) for @tags:{...} queries.
    Sanitized tags only ever contain [A-Za-z0-9_-], so in practice this escapes '-'."""
    return re.sub(r"([^a-zA-Z0-9_])", r"\\\1", tag)

def _parse_search_results(raw):
    """Normalize an FT.SEARCH reply across redis-py response formats.

    Returns (total, [(redis_key, {field: value}), ...]).
    Handles both the RESP3 map reply (redis-py 5+/RESP3: dict with 'results') and
    the legacy RESP2 flat list ([count, key, [f, v, ...], ...])."""
    if isinstance(raw, dict):
        total = raw.get(b"total_results", raw.get("total_results", 0)) or 0
        docs = raw.get(b"results", raw.get("results", [])) or []
        out = []
        for doc in docs:
            rid = doc.get(b"id", doc.get("id"))
            attrs = doc.get(b"extra_attributes", doc.get("extra_attributes", {})) or {}
            fd = {_decode(k): _decode(v) for k, v in attrs.items()}
            out.append((_decode(rid), fd))
        return int(total), out
    # Legacy RESP2 flat list
    total = raw[0] if raw else 0
    out = []
    items = raw[1:]
    for i in range(0, len(items), 2):
        redis_key = _decode(items[i])
        fields = items[i + 1]
        fd = {_decode(fields[j]): _decode(fields[j + 1]) for j in range(0, len(fields), 2)}
        out.append((redis_key, fd))
    return int(total), out


# ── KV tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def kv_set(key: str, value: str, label: str = "", tags: str = "", ttl_days: int = 90, shared: bool = False) -> str:
    """Store a key/value fact — instant lookup, no embeddings.
    Use for discrete facts with a known name: config, settings, versions, names,
    non-secret connection info.

    NOT a secrets store. This backend has no per-client authentication and, by
    default, no Redis password, so anything stored here is readable by every
    other agent/client that can reach the same Redis. Do NOT put API keys,
    passwords, tokens, or other secrets in it — keep those in a real secret
    manager or environment variables; at most store a non-secret *reference*
    to where the secret lives.

    Parameters:
    - key (required): Unique identifier. Use slugs like 'prod-db-host', 'user-timezone'.
      Saving with an existing key overwrites the previous value.
    - value (required): The value to store — any non-secret string (URL host, number, JSON, etc).
    - label: Short human-readable description (shown in lists). Example: 'Production DB host'.
    - tags: Comma-separated labels for grouping. Example: 'db,production'.
    - ttl_days: OMIT this parameter in most cases — default is 90 days and TTL resets on
      every read so popular facts never expire. Only set explicitly when needed:
      ttl_days=365 for long-lived facts, ttl_days=7 for temporary context.
      Do NOT pass ttl_days=0 unless the fact must be permanent (no expiry ever).
    - shared: OMIT unless this server is running with NAMESPACE set AND the fact
      should be visible to every other agent/namespace on this instance, not just
      this one. Default (false) stores in this server's own area. NAMESPACE and
      shared are a cooperative key-prefix convention, not a security boundary —
      any client can choose any NAMESPACE or pass shared=True.

    Examples: kv_set('prod-db-host', 'db.internal:5432', label='Production DB host', tags='db')
              kv_set('user-language', 'Russian', label='User preferred language', ttl_days=365)
    """
    _, kv_prefix, _ = _scope(shared)
    r = _redis()
    try:
        redis_key = f"{kv_prefix}{key}"
        safe_tags = ",".join(_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t)) if tags else ""
        mapping = {
            b"value":     value.encode(),
            b"tags":      safe_tags.encode(),
            b"timestamp": str(int(time.time())).encode(),
            b"ttl_days":  str(ttl_days).encode(),
        }
        if label: mapping[b"label"] = label.encode()
        await r.hset(redis_key, mapping=mapping)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
    finally:
        await r.aclose()

    ttl_info = f"ttl={ttl_days}d (resets on read)" if ttl_days > 0 else "no expiry"
    desc = f" ({label})" if label else ""
    return f"Stored kv[{key}]{desc} = {value[:80]}" + (f"  tags=[{safe_tags}]" if safe_tags else "") + f"  {ttl_info}"


@mcp.tool()
async def kv_get(key: str, shared: bool = False) -> str:
    """Retrieve a value by its exact key — O(1), instant, always consistent.
    Automatically refreshes the TTL on read, so frequently accessed facts never expire.

    Parameters:
    - key (required): The exact key used when calling kv_set.
      Example: kv_get('prod-db-url') → 'postgresql://...'
    - shared: Must match how the entry was saved — pass the same `shared` value
      used in the kv_set call, or this looks in the wrong area and reports
      "Not found" even though the key exists elsewhere. There is no automatic
      fallback between areas.
    """
    _, kv_prefix, _ = _scope(shared)
    r = _redis()
    try:
        redis_key = f"{kv_prefix}{key}"
        data = await r.hgetall(redis_key)
        if not data:
            return f"Not found: '{key}'"
        # Refresh TTL on read
        ttl_days = int(_decode(data.get(b"ttl_days", b"90")) or 90)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
        ttl_left = await r.ttl(redis_key)
    finally:
        await r.aclose()

    value = _decode(data.get(b"value", b""))
    label = _decode(data.get(b"label", b""))
    tags  = _decode(data.get(b"tags",  b""))
    ts    = _fmt_ts(data.get(b"timestamp", b"0"))
    desc = f" ({label})" if label else ""
    result = f"kv[{key}]{desc} = {value}\nsaved: {ts}  ttl: {_fmt_ttl(ttl_left)} remaining"
    if tags:
        result += f"  tags=[{tags}]"
    return result


@mcp.tool()
async def kv_delete(key: str, shared: bool = False) -> str:
    """Delete a key/value entry by its exact key.

    Parameters:
    - key (required): The exact key to delete. Cannot be undone.
    - shared: Must match how the entry was saved (see kv_get) — deletes from
      that area only.
    """
    _, kv_prefix, _ = _scope(shared)
    r = _redis()
    try:
        deleted = await r.delete(f"{kv_prefix}{key}")
    finally:
        await r.aclose()
    return f"Deleted kv[{key}]" if deleted else f"Not found: '{key}'"


@mcp.tool()
async def kv_list(tag: str = "", pattern: str = "", shared: bool = False) -> str:
    """List stored key/value entries with their TTL.

    Parameters:
    - tag: Filter by tag (e.g. tag='db').
    - pattern: Glob pattern for key names (e.g. pattern='prod-*').
    - shared: List this server's own area (default) or the always-available
      shared area (shared=True). Lists one area at a time, never both.
    """
    _, kv_prefix, _ = _scope(shared)
    r = _redis()
    try:
        glob = f"{kv_prefix}{pattern}*" if pattern else f"{kv_prefix}*"
        keys = [k async for k in r.scan_iter(glob, count=200)]

        results = []
        for k in sorted(keys):
            data = await r.hgetall(k)
            ttl_left = await r.ttl(k)
            name  = _decode(k).replace(kv_prefix, "")
            value = _decode(data.get(b"value", b""))
            label = _decode(data.get(b"label", b""))
            tags_ = _decode(data.get(b"tags",  b""))
            ts    = _fmt_ts(data.get(b"timestamp", b"0"))
            if tag and tag not in tags_.split(","):
                continue
            desc = f" ({label})" if label else ""
            line = f"[{ts} | ttl:{_fmt_ttl(ttl_left)}] {name}{desc} = {value[:60]}"
            if tags_:
                line += f"  [{tags_}]"
            results.append(line)
    finally:
        await r.aclose()

    return "\n".join(results) if results else "No key/value entries found."


# ── Semantic Memory tools ─────────────────────────────────────────────────────

@mcp.tool()
async def mem_save(text: str, label: str = "", code: str = "", tags: str = "", ttl_days: int = 90, shared: bool = False) -> str:
    """Save a fact to semantic memory with a vector embedding for similarity search.
    Use for knowledge that needs to be found by meaning: decisions, patterns, context, docs.

    Parameters:
    - text (required): Full human-readable description. Written as a complete sentence.
      Example: "We use JWT with 24h expiry. Refresh tokens stored in Redis with 30d TTL."
    - label: Short human-readable description (shown in lists and search results).
      Example: "JWT refresh token strategy". Keep under 60 chars.
    - code: Code snippet or structured data associated with this fact.
    - tags: Comma-separated labels for pre-filtering. Example: "auth,jwt,backend".
    - ttl_days: OMIT this parameter in most cases — default is 90 days and TTL resets on
      every search hit so popular facts never expire. Only set explicitly when needed:
      ttl_days=365 for long-lived facts, ttl_days=7 for temporary context.
      Do NOT pass ttl_days=0 unless the fact must be permanent (no expiry ever).
    - shared: OMIT unless this server is running with NAMESPACE set AND the fact
      should be visible to every other agent/namespace on this instance, not just
      this one. Default (false) stores in this server's own area.

    Returns the memory ID (use mem_delete to remove it).
    """
    mem_prefix, _, index = _scope(shared)
    embed_input = f"{text}\n{code}" if code else text
    vector_bytes = _encode(await _embed(embed_input))
    mid = str(uuid.uuid4())

    r = _redis()
    try:
        await _ensure_index(r, index, mem_prefix)
        redis_key = f"{mem_prefix}{mid}"
        mapping = {
            b"text":      text.encode(),
            b"vector":    vector_bytes,
            b"timestamp": str(int(time.time())).encode(),
            b"ttl_days":  str(ttl_days).encode(),
        }
        safe_tags = ",".join(_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t)) if tags else ""
        if label:     mapping[b"label"] = label.encode()
        if code:      mapping[b"code"]  = code.encode()
        if safe_tags: mapping[b"tags"]  = safe_tags.encode()
        await r.hset(redis_key, mapping=mapping)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
    finally:
        await r.aclose()

    display = f"'{label}'" if label else f"'{text[:60]}'"
    parts = [f"label={display}"]
    if code:      parts.append(f"code='{code[:30]}'")
    if safe_tags: parts.append(f"tags=[{safe_tags}]")
    ttl_info = f"ttl={ttl_days}d (resets on hit)" if ttl_days > 0 else "no expiry"
    return f"Saved mem[{mid}] {', '.join(parts)}  {ttl_info}"


@mcp.tool()
async def mem_search(query: str, tags: str = "", top_k: int = 5, shared: bool = False) -> str:
    """Search semantic memory by meaning — finds relevant facts even without exact word matches.
    Automatically refreshes TTL for every result, so popular memories never expire.

    Parameters:
    - query (required): Natural language question or topic.
    - tags: Comma-separated tag pre-filter. Example: tags="auth,backend".
    - top_k: Number of results (default 5).
    - shared: Search this server's own area (default) or the always-available
      shared area (shared=True). Searches one area at a time — call twice
      (shared=False then shared=True) to check both; results are never merged.

    Call at the start of conversations to load relevant context.
    Results show similarity %, TTL remaining, tags, and memory ID.
    """
    mem_prefix, _, index = _scope(shared)
    vector_bytes = _encode(await _embed(query))

    if tags:
        tag_filter = "|".join(_escape_tag(_sanitize_tag(t)) for t in tags.split(",") if _sanitize_tag(t))
        ft_query = f"(@tags:{{{tag_filter}}})=>[KNN {top_k} @vector $vec AS score]"
    else:
        ft_query = f"*=>[KNN {top_k} @vector $vec AS score]"

    r = _redis()
    try:
        await _ensure_index(r, index, mem_prefix)
        raw = await r.execute_command(
            "FT.SEARCH", index, ft_query,
            "PARAMS", "2", "vec", vector_bytes,
            "RETURN", "7", "label", "text", "code", "tags", "timestamp", "score", "ttl_days",
            "SORTBY", "score",
            "DIALECT", "2",
        )

        total, docs = _parse_search_results(raw)
        if total == 0 or not docs:
            return "No memories found."

        results = []
        for redis_key, fd in docs:
            mid = redis_key.replace(mem_prefix, "")

            # Refresh TTL on hit
            ttl_days = int(fd.get("ttl_days", "90") or 90)
            if ttl_days > 0:
                await r.expire(redis_key, ttl_days * 86400)
            ttl_left = await r.ttl(redis_key)

            sim  = round((1 - float(fd.get("score", 1.0))) * 100, 1)
            dt   = _fmt_ts(fd.get("timestamp", 0))
            label = fd.get("label") or fd.get("text", "")[:60]
            head = f"[{sim}% | {dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid}"
            if fd.get("tags"): head += f"  tags=[{fd['tags']}]"
            body = fd.get("text", "")
            if fd.get("code"): body += f"\n```\n{fd['code']}\n```"
            results.append(f"{head}\n{body}")
    finally:
        await r.aclose()

    return "\n\n---\n".join(results)


@mcp.tool()
async def mem_list(limit: int = 20, tag: str = "", shared: bool = False) -> str:
    """Browse semantic memories sorted by recency with TTL info.

    Parameters:
    - limit: Maximum number of results (default 20).
    - tag: Filter by a single tag. Example: tag='auth'.
    - shared: Browse this server's own area (default) or the always-available
      shared area (shared=True). Lists one area at a time, never both.
    """
    mem_prefix, _, index = _scope(shared)
    r = _redis()
    try:
        await _ensure_index(r, index, mem_prefix)
        if tag:
            safe_tag = _escape_tag(_sanitize_tag(tag))
            raw = await r.execute_command(
                "FT.SEARCH", index, f"@tags:{{{safe_tag}}}",
                "RETURN", "5", "label", "text", "tags", "timestamp", "ttl_days",
                "LIMIT", "0", str(limit),
                "SORTBY", "timestamp", "DESC",
            )
            results = []
            _, docs = _parse_search_results(raw)
            for redis_key, fd in docs:
                mid = redis_key.replace(mem_prefix, "")
                ttl_left = await r.ttl(redis_key)
                dt = _fmt_ts(fd.get("timestamp", 0))
                label = fd.get("label") or fd.get("text", "")[:60]
                line = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid}"
                if fd.get("tags"): line += f"  [{fd['tags']}]"
                line += f"\n{fd.get('text','')[:100]}"
                results.append(line)
        else:
            keys = [k async for k in r.scan_iter(f"{mem_prefix}*", count=100)][:limit]
            results = []
            for k in keys:
                data = await r.hgetall(k)
                if b"vector" not in data:
                    continue
                mid   = _decode(k).replace(mem_prefix, "")
                label_ = _decode(data.get(b"label", b""))
                text  = _decode(data.get(b"text",  b""))
                tags_ = _decode(data.get(b"tags",  b""))
                ttl_left = await r.ttl(k)
                dt    = _fmt_ts(data.get(b"timestamp", b"0"))
                label = label_ or text[:60]
                line  = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid}"
                if tags_: line += f"  [{tags_}]"
                line += f"\n{text[:100]}"
                results.append(line)
    finally:
        await r.aclose()

    return "\n\n".join(results) if results else "No semantic memories found."


@mcp.tool()
async def mem_delete(memory_id: str, shared: bool = False) -> str:
    """Permanently delete a semantic memory by its ID.

    Parameters:
    - memory_id (required): Full UUID or short prefix from mem_save / mem_search / mem_list output.
    - shared: Must match how the entry was saved (see mem_search) — deletes
      from that area only.
    """
    mem_prefix, _, _ = _scope(shared)
    memory_id = memory_id.strip().lower()
    is_full_uuid = bool(_UUID_RE.match(memory_id))

    if not is_full_uuid and not _HEX_PREFIX_RE.match(memory_id):
        return "Invalid memory ID: expected full UUID or short hex prefix (1-8 chars)."

    r = _redis()
    try:
        if is_full_uuid:
            deleted = await r.delete(f"{mem_prefix}{memory_id}")
            if deleted:
                return f"Deleted mem[{memory_id}]"
            return f"Not found: '{memory_id}'"

        pattern = f"{mem_prefix}{memory_id}*"
        matches: list[bytes] = []
        cursor = 0
        for _ in range(_MAX_SCAN_ROUNDS):
            cursor, keys = await r.scan(cursor, match=pattern, count=200)
            for k in keys:
                if len(matches) >= _MAX_PREFIX_MATCHES:
                    return f"Too many matches for prefix '{memory_id}'. Use full UUID."
                matches.append(k)
            if cursor == 0:
                break
        else:
            return f"Scan round limit reached for prefix '{memory_id}'. Use full UUID."
        if len(matches) == 0:
            return f"Not found: '{memory_id}'"
        if len(matches) > 1:
            ids = ", ".join(_decode(k).replace(mem_prefix, "") for k in matches[:5])
            return f"Ambiguous ID '{memory_id}', multiple matches: {ids}. Use full UUID."
        deleted = await r.delete(matches[0])
        full_id = _decode(matches[0]).replace(mem_prefix, "")
        return f"Deleted mem[{full_id}]" if deleted else f"Not found: '{memory_id}'"
    finally:
        await r.aclose()


# ── Unified search ────────────────────────────────────────────────────────────

@mcp.tool()
async def search(query: str, tags: str = "", top_k: int = 5, shared: bool = False) -> str:
    """Search ALL memory at once — both key-value and semantic.
    Use this as the default search tool. Combines results from both stores.

    Parameters:
    - query (required): Natural language question, topic, or key name.
    - tags: Comma-separated tag pre-filter.
    - top_k: Max semantic results (default 5). All matching kv entries are always included.
    - shared: Search this server's own area (default) or the always-available
      shared area (shared=True). Searches one area at a time — call twice to
      check both; results are never merged.

    Returns kv matches (by key/value substring) + semantic matches (by meaning), clearly separated.
    """
    _, kv_prefix, _ = _scope(shared)
    parts = []

    # 1. Search kv by substring in key and value
    r = _redis()
    try:
        kv_results = []
        q_lower = query.lower()
        async for k in r.scan_iter(f"{kv_prefix}*", count=200):
            data = await r.hgetall(k)
            name  = _decode(k).replace(kv_prefix, "")
            value = _decode(data.get(b"value", b""))
            label = _decode(data.get(b"label", b""))
            tags_ = _decode(data.get(b"tags",  b""))
            if tags:
                filter_tags = {_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t)}
                entry_tags = set(tags_.split(",")) if tags_ else set()
                if not filter_tags & entry_tags:
                    continue
            if q_lower in name.lower() or q_lower in value.lower() or q_lower in label.lower():
                ttl_left = await r.ttl(k)
                ttl_days = int(_decode(data.get(b"ttl_days", b"90")) or 90)
                if ttl_days > 0:
                    await r.expire(k, ttl_days * 86400)
                dt = _fmt_ts(data.get(b"timestamp", b"0"))
                desc = f" ({label})" if label else ""
                line = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {name}{desc} = {value[:80]}"
                if tags_: line += f"  [{tags_}]"
                kv_results.append(line)
    finally:
        await r.aclose()

    if kv_results:
        parts.append("── Key-Value matches ──\n" + "\n".join(kv_results))

    # 2. Semantic search
    mem_result = await mem_search(query=query, tags=tags, top_k=top_k, shared=shared)
    if mem_result and mem_result != "No memories found.":
        parts.append("── Semantic matches ──\n" + mem_result)

    if not parts:
        return "Nothing found in any memory store."

    return "\n\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
