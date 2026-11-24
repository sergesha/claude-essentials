"""Official MCP SDK stdio bridge over the same authenticated HTTP client."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from threading import Lock
from pydantic import Field, ValidationError, create_model
from .api_models import MODELS, OpenLocation, Payload, parse_request
from .client import AtlasClient
from .compose_client import ComposeClient
from .contracts import AtlasError
from .launcher import ensure_service, load_config, service_context


class InstanceSettings(Payload):
    project: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9_-]*$',
                                description='Compose project to use for this MCP connection')


class ProjectLocation(InstanceSettings, OpenLocation):
    pass


def default_project():
    project = os.getenv('KNOWLEDGE_ATLAS_DEFAULT_PROJECT') or os.getenv('KNOWLEDGE_ATLAS_DOCKER_PROJECT')
    settings = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'knowledge-atlas/mcp.json'
    try:
        if project:
            return InstanceSettings(project=project).project
        return InstanceSettings.model_validate_json(settings.read_text()).project
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as error:
        raise AtlasError('invalid_configuration', 'invalid Knowledge Atlas MCP instance settings') from error


MCP_MODELS = {**MODELS, 'atlas.open': create_model('OpenProjectRequest', __base__=MODELS['atlas.open'],
              payload=(ProjectLocation, Field(default_factory=ProjectLocation)))}


class SessionClient:
    def __init__(self, config_path, channel, project=None):
        self.config_path = config_path
        self.channel = channel
        self.project = project
        self.local = None
        self.local_atlas_path = None
        self.connections = {}
        self.lock = Lock()

    def _local(self, *, require_bound_atlas):
        if self.local is None:
            config = load_config(self.config_path)
            context = service_context(config)
            bound_path = context.configuration.get('atlas_path')
        else:
            bound_path = self.local_atlas_path
        if require_bound_atlas and not bound_path:
            raise AtlasError('invalid_request',
                             'Choose a Compose instance with payload.project or open a native atlas with payload.path.',
                             422)
        if self.local is None:
            ensure_service(config)
            self.local = AtlasClient(context.service_root, channel=self.channel)
            self.local_atlas_path = bound_path
        return self.local

    def call(self, request):
        selected = None
        if request.action == 'atlas.open' and 'project' in request.payload:
            payload = dict(request.payload)
            selected = payload.pop('project')
            request = replace(request, payload=payload)
        with self.lock:
            project = selected or self.project
            if project is None:
                client = self._local(require_bound_atlas=request.action == 'atlas.open'
                                     and request.payload.get('path') is None)
            elif project in self.connections:
                client = self.connections[project]
            else:
                client = ComposeClient(project, self.channel)
                self.connections[project] = client
        try:
            result = client.call(request)
        except AtlasError as error:
            if project and error.code == 'backend_unavailable':
                with self.lock:
                    if self.connections.get(project) is client:
                        del self.connections[project]
                        client.close()
            raise
        if selected is not None:
            with self.lock:
                self.project = selected
        return result

    def close(self):
        for client in self.connections.values():
            client.close()


def tools_for():
    open_description = ('Open a Knowledge Atlas. Pass payload.project to select a running Compose project '
                        'or payload.path to open an unbound native atlas. Omit arguments when this MCP '
                        'connection already has a Compose project or the native service has a configured atlas path.')
    return [{'name':'atlas_'+name.replace('.','_'),
             'description':open_description if name == 'atlas.open' else 'Knowledge Atlas '+name,
             'inputSchema':model.model_json_schema()} for name,model in MCP_MODELS.items()]


def call_tool(client,name,arguments):
    try:
        action=next((a for a in MCP_MODELS if 'atlas_'+a.replace('.','_')==name),None)
        if action is None:raise AtlasError('unsupported_action',name,422)
        request=parse_request(action,arguments if arguments is not None else {}, models=MCP_MODELS)
        return json.loads(json.dumps(asdict(client.call(request)),default=str))
    except AtlasError as error:return {'error':{'code':error.code,'message':str(error),'status':error.status}}


async def serve(client):
    import asyncio
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool,TextContent,CallToolResult
    server=Server('knowledge-atlas')
    @server.list_tools()
    async def list_tools():return [Tool(**item) for item in tools_for()]
    @server.call_tool(validate_input=False)
    async def invoke(name,arguments):
        result=await asyncio.to_thread(call_tool,client,name,arguments)
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(result))],
                              structuredContent=result,isError=result.get('error') is not None)
    try:
        async with stdio_server() as (reader,writer):
            await server.run(reader,writer,server.create_initialization_options())
    finally:
        await asyncio.to_thread(client.close)
