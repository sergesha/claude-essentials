"""Scope isolation and compatibility checks; optional Redis Stack integration.

Run with pytest. Set REDIS_SCOPE_TEST_URL to enable isolated backend fixtures.
Only synthetic keys and indexes belonging to these fixtures are deleted.
"""

import asyncio
import importlib.util
import inspect
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import redis.asyncio as redis
from redis.exceptions import ResponseError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


spec = importlib.util.spec_from_file_location(
    "scope_test_server", Path(__file__).parents[1] / "server" / "memory_mcp.py"
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

TOOLS = {
    "kv_set": {"key": "status", "value": "active"},
    "kv_get": {"key": "status"},
    "kv_delete": {"key": "status"},
    "kv_list": {},
    "mem_save": {"text": "a fact"},
    "mem_search": {"query": "a fact"},
    "mem_list": {},
    "mem_delete": {"memory_id": "12345678-1234-1234-1234-123456789abc"},
    "search": {"query": "a fact"},
}


def configure(monkeypatch, namespace, base=""):
    monkeypatch.setattr(server, "NAMESPACE", namespace)
    monkeypatch.setattr(server, "_BASE_MEM", f"{base}mem:")
    monkeypatch.setattr(server, "_BASE_KV", f"{base}kv:")
    monkeypatch.setattr(server, "_BASE_INDEX", f"idx:{base}memories")
    monkeypatch.setattr(server, "_OWN_MEM", f"ns:{namespace}:mem:" if namespace else f"{base}mem:")
    monkeypatch.setattr(server, "_OWN_KV", f"ns:{namespace}:kv:" if namespace else f"{base}kv:")
    monkeypatch.setattr(server, "_OWN_INDEX", f"idx:{base}memories:{namespace}" if namespace else f"idx:{base}memories")


@pytest.mark.parametrize("name", TOOLS)
def test_all_tools_accept_optional_scope_without_changing_existing_defaults(name):
    parameters = inspect.signature(getattr(server, name)).parameters
    assert "scope" in parameters, f"{name} does not support scope"
    assert parameters["scope"].default is None
    assert parameters["shared"].default is False


@pytest.mark.parametrize("namespace,shared,expected", [
    ("alpha", False, ("ns:alpha:mem:", "ns:alpha:kv:", "idx:memories:alpha")),
    ("alpha", True, ("mem:", "kv:", "idx:memories")),
    ("", False, ("mem:", "kv:", "idx:memories")),
    ("", True, ("mem:", "kv:", "idx:memories")),
])
def test_omitted_scope_preserves_existing_area(monkeypatch, namespace, shared, expected):
    configure(monkeypatch, namespace)
    assert server._scope(shared) == expected
    assert server._scope(shared, None) == expected


@pytest.mark.parametrize("namespace,shared,expected", [
    ("alpha", False, ("ns:alpha:scope:Ab_9-z:mem:", "ns:alpha:scope:Ab_9-z:kv:", "idx:memories:scope:alpha:Ab_9-z")),
    ("alpha", True, ("ns::scope:Ab_9-z:mem:", "ns::scope:Ab_9-z:kv:", "idx:memories:scope::Ab_9-z")),
    ("", False, ("ns::scope:Ab_9-z:mem:", "ns::scope:Ab_9-z:kv:", "idx:memories:scope::Ab_9-z")),
    ("", True, ("ns::scope:Ab_9-z:mem:", "ns::scope:Ab_9-z:kv:", "idx:memories:scope::Ab_9-z")),
])
def test_scope_is_relative_to_existing_area(monkeypatch, namespace, shared, expected):
    configure(monkeypatch, namespace)
    assert server._scope(shared, "Ab_9-z") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("scope", ["", "*", "?", "[abc]", "a:b", "a/b", "a\\b", " a", "a\n", "комната", "x" * 129])
async def test_invalid_scope_fails_before_backend_or_embedding(monkeypatch, name, scope):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid scope reached an external service")

    monkeypatch.setattr(server, "_redis", forbidden)
    monkeypatch.setattr(server, "_embed", forbidden)
    with pytest.raises(server.ToolError, match="Invalid scope"):
        await getattr(server, name)(**TOOLS[name], scope=scope)


@pytest.mark.asyncio
async def test_real_mcp_catalog_exposes_optional_scope_and_rejects_wildcards():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).parents[1] / "server" / "memory_mcp.py")],
    )
    async with asyncio.timeout(30):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert {tool.name for tool in catalog.tools} == set(TOOLS)
                for tool in catalog.tools:
                    assert "scope" in tool.input_schema["properties"]
                    assert "scope" not in tool.input_schema.get("required", [])
                result = await session.call_tool("kv_list", {"scope": "*"})
                assert result.is_error
                assert "Invalid scope" in "".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ResponseError("NOPERM denied"), ConnectionError("unavailable")])
async def test_scoped_index_errors_do_not_become_creation_attempts(error):
    client = AsyncMock()
    client.execute_command.side_effect = [error, pytest.fail]
    with pytest.raises(type(error), match=str(error)):
        await server._scope_index(client, "index", "prefix:", create=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", [False, True])
async def test_scope_index_validates_redis_reply_formats(mapping):
    info = {b"index_definition": {b"key_type": b"HASH", b"prefixes": [b"expected:"]}} if mapping else [b"index_definition", [b"key_type", b"HASH", b"prefixes", [b"expected:"]]]
    client = AsyncMock()
    client.execute_command.return_value = info
    assert await server._scope_index(client, "index", "expected:")
    with pytest.raises(server.ToolError, match="index.*prefix"):
        await server._scope_index(client, "index", "different:")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [("mem_search", {"query": "fact"}), ("mem_list", {"tag": "fixture"})])
async def test_outside_search_results_are_rejected_without_refreshing_ttl(monkeypatch, name, arguments):
    from memory_index import MemoryIndex
    from embedding import Profile
    import struct
    client = AsyncMock()
    prefix, _, index = server._scope(False, 'test')
    if name == 'mem_search':
        store = MemoryIndex(client, prefix, index, Profile())
        monkeypatch.setattr(store, 'ensure', AsyncMock(return_value=True))
        client.execute_command.side_effect = [[b'num_docs', 1], [1, b'outside:mem:id', [b'parent', b'id', b'generation', b'g', b'score', b'0']]]
        with pytest.raises(ValueError, match='outside'):
            await store.search(struct.pack('<256f',1,*([0]*255)), '', 5)
        client.eval.assert_not_awaited()
    else:
        async def keys(*a, **kw):yield b'outside:mem:id'
        client.scan_iter = keys
        client.exists.return_value = False
        monkeypatch.setattr(server, '_redis', lambda:client)
        with pytest.raises(server.ToolError, match='outside'):
            await server.mem_list(scope='test')
        client.ttl.assert_not_awaited()


@pytest_asyncio.fixture
async def backend(monkeypatch):
    url = os.environ.get("REDIS_SCOPE_TEST_URL")
    if not url:
        pytest.skip("set REDIS_SCOPE_TEST_URL for Redis Stack integration")
    fixture_id = uuid.uuid4().hex
    namespace = f"scope_test_{fixture_id}"
    base = f"scope_test_{fixture_id}:"
    configure(monkeypatch, namespace, base)
    monkeypatch.setattr(server, "_redis", lambda: redis.from_url(url, decode_responses=False))

    async def embedding(text):
        return [1.0] + [0.0] * 767

    monkeypatch.setattr(server, "_embed", embedding)
    class Encoder:
        def __init__(self, *a, **kw):pass
        async def record(self, *a):return [server._encode([1.0]+[0.0]*255)]*2
        async def query(self, *a):return server._encode([1.0]+[0.0]*255)
        async def aclose(self):pass
    monkeypatch.setattr(server, 'Embedder', Encoder)

    client = redis.from_url(url, decode_responses=False)
    await client.ping()
    scopes = [f"a_{fixture_id}", f"b_{fixture_id}"]
    try:
        yield client, scopes
    finally:
        indexes = [_decode(i) for i in await client.execute_command("FT._LIST")]
        for index in indexes:
            if index.startswith(f"idx:{base}"):
                await client.execute_command("FT.DROPINDEX", index)
        patterns = [f"ns:{namespace}:*", f"{base}*"]
        patterns.extend(f"ns::scope:{scope}:*" for scope in scopes)
        for pattern in patterns:
            keys = [key async for key in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


async def indexed(query, expected):
    for _ in range(100):
        result = await query()
        if expected in result:
            return result
        await asyncio.sleep(0.02)
    pytest.fail("fixture memory did not become searchable")


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_kv_crud_and_lists_stay_in_selected_scope(backend, shared):
    client, (a, b) = backend
    await server.kv_set("status", "parent-value", shared=shared)
    await server.kv_set("status", "scope-a-value", shared=shared, scope=a)
    await server.kv_set("status", "scope-b-value", shared=shared, scope=b)
    assert "parent-value" in await server.kv_get("status", shared=shared)
    assert "scope-a-value" in await server.kv_get("status", shared=shared, scope=a)
    assert "scope-b-value" in await server.kv_get("status", shared=shared, scope=b)
    parent_list = await server.kv_list(pattern="*", shared=shared)
    assert "parent-value" in parent_list
    assert "scope-a-value" not in parent_list and "scope-b-value" not in parent_list
    a_list = await server.kv_list(pattern="*", shared=shared, scope=a)
    assert "scope-a-value" in a_list and "scope-b-value" not in a_list
    assert "Not found" in await server.kv_get(f"../scope:{b}:kv:status", shared=shared, scope=a)
    assert "Deleted" in await server.kv_delete("status", shared=shared, scope=a)
    assert "Not found" in await server.kv_get("status", shared=shared, scope=a)
    assert "scope-b-value" in await server.kv_get("status", shared=shared, scope=b)
    assert "parent-value" in await server.kv_get("status", shared=shared)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_semantic_search_list_delete_and_unified_search_are_isolated(backend, shared):
    client, (a, b) = backend
    for scope, text in [(None, "parent-marker"), (a, "scope-a-marker"), (b, "scope-b-marker")]:
        await server.mem_save(text, tags="fixture", shared=shared, scope=scope)
        await server.kv_set("marker", text, shared=shared, scope=scope)
    for scope, marker, forbidden in [(None, "parent-marker", "scope-a-marker"), (a, "scope-a-marker", "scope-b-marker")]:
        kwargs = {"shared": shared, "scope": scope}
        result = await indexed(lambda: server.mem_search("marker", **kwargs), marker)
        assert forbidden not in result
        assert marker in await server.mem_list(tag="fixture", **kwargs)
        assert forbidden not in await server.mem_list(**kwargs)
        combined = await server.search("marker", **kwargs)
        assert "Key-Value matches" in combined and "Semantic matches" in combined
        assert marker in combined and forbidden not in combined
    a_prefix, _, _ = server._scope(shared, a)
    memory_key = await anext(client.scan_iter(match=f"{a_prefix}*"))
    memory_id = _decode(memory_key)[len(a_prefix):]
    assert "Not found" in await server.mem_delete(memory_id, shared=shared, scope=b)
    assert "Deleted" in await server.mem_delete(memory_id[:8], shared=shared, scope=a)
    assert "scope-b-marker" in await server.mem_list(shared=shared, scope=b)


@pytest.mark.asyncio
async def test_shared_scope_matches_empty_namespace_and_survives_parallel_calls(backend, monkeypatch):
    client, (a, b) = backend
    await asyncio.gather(
        server.kv_set("status", "own-a", scope=a),
        server.kv_set("status", "shared-a", scope=a, shared=True),
        server.kv_set("status", "own-b", scope=b),
    )
    assert "own-a" in await server.kv_get("status", scope=a)
    assert "own-b" in await server.kv_get("status", scope=b)
    monkeypatch.setattr(server, "NAMESPACE", "")
    assert "shared-a" in await server.kv_get("status", scope=a)
    assert "shared-a" in await server.kv_get("status", scope=a, shared=True)


@pytest.mark.asyncio
async def test_scoped_reads_do_not_create_indexes(backend):
    client, (a, b) = backend
    _, _, index = server._scope(False, a)
    assert "No memories" in await server.mem_search("missing", scope=a)
    assert "No semantic" in await server.mem_list(scope=a)
    assert "Nothing found" in await server.search("missing", scope=a)
    assert index not in [_decode(i) for i in await client.execute_command("FT._LIST")]


@pytest.mark.asyncio
async def test_scoped_reads_refresh_ttl_as_before(backend):
    client, (a, b) = backend
    await server.kv_set("status", "value", ttl_days=7, scope=a)
    _, kv_prefix, _ = server._scope(False, a)
    await client.expire(f"{kv_prefix}status", 60)
    await server.kv_get("status", scope=a)
    assert await client.ttl(f"{kv_prefix}status") > 6 * 86400
    await server.mem_save("ttl-marker", ttl_days=7, scope=a)
    mem_prefix, _, _ = server._scope(False, a)
    key = await anext(client.scan_iter(match=f"{mem_prefix}*"))
    await client.expire(key, 60)
    await indexed(lambda: server.mem_search("ttl-marker", scope=a), "ttl-marker")
    assert await client.ttl(key) > 6 * 86400


@pytest.mark.asyncio
async def test_wrong_index_prefix_is_rejected_before_disclosing_data(backend):
    client, (a, b) = backend
    await server.mem_save("scope-b-secret", scope=b)
    a_prefix, _, a_index = server._scope(False, a)
    b_prefix, _, _ = server._scope(False, b)
    from memory_index import MemoryIndex
    from embedding import Profile
    target = MemoryIndex(client, a_prefix, a_index, Profile())
    foreign = MemoryIndex(client, b_prefix, a_index, Profile())
    # Give the selected index another area's prefix while preserving vector schema.
    await foreign.ensure_index()
    await client.hset(target.state,mapping={'status':'active','profile':Profile().fingerprint})
    for call in [lambda: server.mem_save('fact',scope=a),lambda:server.mem_search('fact',scope=a),lambda:server.mem_list(scope=a)]:
        with pytest.raises(server.ToolError,match='Index.*area|Index.*profile'):
            await call()


@pytest.mark.asyncio
async def test_concurrent_first_semantic_writes_share_one_index(backend):
    client, (a, b) = backend
    await asyncio.gather(*(server.mem_save(f"parallel-{i}", scope=a) for i in range(5)))
    mem_prefix, _, index = server._scope(False, a)
    keys = [key async for key in client.scan_iter(match=f"{mem_prefix}*")]
    assert len(keys) == 5
