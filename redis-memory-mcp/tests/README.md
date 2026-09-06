# Redis Memory checks

`smoke_mcp.py` checks an already-built image's MCP handshake and tool catalog
without connecting to Redis (install the test client with `pip install 'mcp>=2,<3'`):

```bash
python tests/smoke_mcp.py IMAGE
```

`real_codex_host.py` is an opt-in real-host integration check. It requires
Codex, Docker, Python 3.11+, network access for the launcher download, and an
existing Redis Stack + TEI backend reachable at `host.docker.internal:6379` and
`host.docker.internal:8081`. Redis must be the local `redis-stack` container with
unauthenticated test access, including `FT.DROPINDEX` for fixture cleanup:

```bash
python tests/real_codex_host.py .
```

The real-host check installs a copied plugin into isolated temporary
`HOME`/`CODEX_HOME`, starts it through the Codex app-server protocol, and calls
all nine MCP tools. It writes only synthetic one-day fixtures under a unique
namespace, deletes them, removes its temporary image and RediSearch index, and
never records MCP result content. It also performs a redacted, read-only
`mem_list(limit=1, shared=true)` call; existing shared-memory content is not
printed or saved.
