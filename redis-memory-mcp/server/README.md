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
