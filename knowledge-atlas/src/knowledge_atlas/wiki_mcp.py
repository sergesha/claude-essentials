"""Serve the pinned native Wiki handlers available to an Atlas authoring session."""
import argparse
import asyncio
from pathlib import Path

WIKI_TOOLS = ('guide', 'search', 'read', 'create', 'edit', 'append', 'lint')


async def restrict_tools(server):
    available = {tool.name for tool in await server.list_tools()}
    if not set(WIKI_TOOLS) <= available:
        raise RuntimeError('native Wiki is missing required authoring tools')
    for name in available.difference(WIKI_TOOLS):
        server.remove_tool(name)


async def serve(workspace):
    from local_server import _init_workspace, _LOCAL_USER_ID
    from mcp.server.fastmcp import FastMCP
    from tools import register
    from vaultfs import SqliteVaultFS

    await _init_workspace(str(workspace.resolve()))
    try:
        server = FastMCP('LLM Wiki', instructions='Call guide first to work with the current Wiki.')
        register(server, lambda ctx: _LOCAL_USER_ID, SqliteVaultFS)
        await restrict_tools(server)
        await server.run_stdio_async()
    finally:
        await SqliteVaultFS.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    asyncio.run(serve(parser.parse_args().workspace))
