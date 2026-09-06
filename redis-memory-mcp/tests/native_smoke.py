"""Verify a wheel and its installed native stdio MCP executable.

Usage: python tests/native_smoke.py WHEEL EXECUTABLE [--redis-kv]
With --redis-kv, requires a Redis instance at REDIS_URL.
"""

import argparse
import asyncio
import configparser
import os
from pathlib import Path
import uuid
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "kv_set",
    "kv_get",
    "kv_list",
    "kv_delete",
    "mem_save",
    "mem_search",
    "mem_list",
    "mem_delete",
    "search",
}


def check_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "memory_mcp.py" in names, "wheel does not contain memory_mcp.py"
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points) == 1, "wheel must contain one entry_points.txt"

        config = configparser.ConfigParser()
        config.read_string(archive.read(entry_points[0]).decode())
        actual = config["console_scripts"]["redis-memory-mcp"]
        assert actual == "memory_mcp:main", f"unexpected console entry point: {actual}"


def result_text(result) -> str:
    assert not result.isError, f"MCP tool failed: {result}"
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def check_executable(executable: str, redis_kv: bool) -> None:
    run_id = uuid.uuid4().hex
    key = f"native-smoke-{run_id}"
    value = f"value-{run_id}"
    env = {
        **os.environ,
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        "NAMESPACE": f"native-smoke-{run_id}",
    }
    server = StdioServerParameters(command=executable, env=env)

    async with asyncio.timeout(60):
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                names = {tool.name for tool in catalog.tools}
                assert names == EXPECTED_TOOLS, f"unexpected MCP tools: {names}"

                if redis_kv:
                    created = False
                    try:
                        stored = result_text(
                            await session.call_tool(
                                "kv_set", {"key": key, "value": value, "ttl_days": 1}
                            )
                        )
                        created = True
                        assert key in stored and value in stored

                        fetched = result_text(
                            await session.call_tool("kv_get", {"key": key})
                        )
                        assert key in fetched and value in fetched

                        deleted = result_text(
                            await session.call_tool("kv_delete", {"key": key})
                        )
                        created = False
                        assert deleted == f"Deleted kv[{key}]"
                    finally:
                        if created:
                            await session.call_tool("kv_delete", {"key": key})

    suffix = " and Redis KV" if redis_kv else ""
    print(f"Native wheel, MCP handshake and {len(names)} tools{suffix}: OK")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("executable")
    parser.add_argument("--redis-kv", action="store_true")
    args = parser.parse_args()

    check_wheel(args.wheel)
    await check_executable(args.executable, args.redis_kv)


if __name__ == "__main__":
    asyncio.run(main())
