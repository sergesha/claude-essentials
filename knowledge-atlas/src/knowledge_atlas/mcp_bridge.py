"""Official MCP SDK stdio bridge over the same authenticated HTTP client."""
from dataclasses import asdict
import json
from .api_models import parse_request
from .contracts import AtlasError


def tools_for(client):
    return [{'name':'atlas_'+name.replace('.','_'),'description':'Knowledge Atlas '+name,
             'inputSchema':value['input']} for name,value in client.schema()['actions'].items()]


def call_tool(client,name,arguments):
    try:
        actions=client.schema()['actions']
        action=next((a for a in actions if 'atlas_'+a.replace('.','_')==name),None)
        if action is None:raise AtlasError('unsupported_action',name,422)
        request=parse_request(action,arguments)
        return json.loads(json.dumps(asdict(client.call(request)),default=str))
    except AtlasError as error:return {'error':{'code':error.code,'message':str(error),'status':error.status}}


async def serve(client):
    import asyncio
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool,TextContent,CallToolResult
    server=Server('knowledge-atlas')
    @server.list_tools()
    async def list_tools():return [Tool(**item) for item in await asyncio.to_thread(tools_for,client)]
    @server.call_tool(validate_input=False)
    async def invoke(name,arguments):
        result=await asyncio.to_thread(call_tool,client,name,arguments)
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(result))],
                              structuredContent=result,isError=result.get('error') is not None)
    async with stdio_server() as (reader,writer):
        await server.run(reader,writer,server.create_initialization_options())
