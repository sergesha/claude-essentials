"""Check the built image's real MCP handshake/catalog, without touching Redis.

Usage: python tests/smoke_mcp.py IMAGE
Requires the MCP v2 Python client. A removed SDK import must fail this check.
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(image: str) -> None:
    server = StdioServerParameters(command="docker", args=["run", "--rm", "-i", image])
    async with asyncio.timeout(60):
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                names = {tool.name for tool in catalog.tools}
                expected = {
                    "kv_set", "kv_get", "kv_list", "kv_delete",
                    "mem_save", "mem_search", "mem_list", "mem_delete", "search",
                }
                assert expected <= names, f"Missing MCP tools: {expected - names}"
                print(f"MCP handshake and {len(names)} tools: OK")


if __name__ == "__main__":
    asyncio.run(check(sys.argv[1]))
