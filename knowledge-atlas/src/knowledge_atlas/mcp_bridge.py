"""Official MCP SDK stdio bridge over the same authenticated HTTP client."""
from dataclasses import asdict
import json
from threading import Lock
from typing import Literal

from . import __version__
from .api_models import MODELS, Payload, parse_request
from .client import AtlasClient
from .container_client import ContainerClient, resolve_container
from .contracts import ActionResult, AtlasError
from .launcher import ensure_service, load_config, reject_removed_deployment_settings, service_context
from .viewer.backend import SERVICE_STATUS_TOOL


class ContainerLocation(Payload):
    container_name: str


class ConnectRequest(Payload):
    action: Literal['atlas.connect'] = 'atlas.connect'
    payload: ContainerLocation


MCP_MODELS = {**MODELS, 'atlas.connect': ConnectRequest}


class SessionClient:
    def __init__(self, config_path, channel, *, attach_existing=False):
        self.config_path = config_path
        self.channel = channel
        self.attach_existing = attach_existing
        self._client = None
        self._container_name = None
        self._closed = False
        self.lock = Lock()

    def _open_local(self):
        config = load_config(self.config_path)
        context = service_context(config)
        if not self.attach_existing:
            ensure_service(config)
        client = AtlasClient(context.service_root, channel=self.channel, pin_identity=True)
        version = client.verify_ready()
        self._client = client
        return version

    def _connect(self, request):
        if self.attach_existing:
            raise AtlasError('unsupported_action', 'atlas.connect is only available on the host MCP connection', 422)
        if (set(request.payload) != {'container_name'}
                or not isinstance(request.payload['container_name'], str)):
            raise AtlasError('invalid_request', 'atlas.connect requires payload.container_name')
        name = request.payload['container_name']
        if self._client is None:
            client = ContainerClient(name, self.channel)
            self._client = client
            version = client.version
        else:
            version = self._repeat_connect(name)
        self._container_name = name
        return ActionResult(data={'container_id': self._client.container_id, 'version': version})

    def _repeat_connect(self, name):
        conflict = AtlasError('service_already_bound', 'use a new MCP connection for another service', 409)
        if self._container_name is None:
            raise conflict
        try:
            target_id = resolve_container(name)
        except AtlasError as error:
            if name == self._container_name or error.code == 'invalid_request':
                raise
            raise conflict from error
        if target_id != self._client.container_id:
            if name == self._container_name:
                raise AtlasError('backend_unavailable',
                                 'the container name now refers to a different ID. Use a new MCP connection.', 503)
            raise conflict
        return self._client.verify_ready()

    def call(self, request):
        with self.lock:
            if self._closed:
                raise AtlasError('backend_unavailable', 'MCP connection is closed', 503)
            if self._client is None and not self.attach_existing:
                reject_removed_deployment_settings()
            if request.action == 'atlas.connect':
                return self._connect(request)
            if self._client is None:
                self._open_local()
            client = self._client
        return client.call(request)

    def attached_status(self):
        with self.lock:
            if self._closed:
                raise AtlasError('backend_unavailable', 'MCP connection is closed', 503)
            version = self._open_local() if self._client is None else self._client.verify_ready()
        return ActionResult(data={'version': version})

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self._container_name is not None:
                self._client.close()


def tools_for(*, attach_existing=False):
    descriptions = {
        'atlas.connect': 'Attach this MCP connection to an already running exact Docker container name.',
        'atlas.open': 'Verify a registered project by its atlas_id UUID.',
    }
    tools = [
        {'name': 'atlas_' + name.replace('.', '_'),
         'description': descriptions.get(name, 'Knowledge Atlas ' + name),
         'inputSchema': model.model_json_schema()}
        for name, model in (MODELS if attach_existing else MCP_MODELS).items()
    ]
    if attach_existing:
        tools.append({'name': SERVICE_STATUS_TOOL, 'description': 'Verify the authenticated Atlas API.',
                      'inputSchema': Payload.model_json_schema()})
    return tools


def call_tool(client, name, arguments, *, attach_existing=False):
    try:
        if attach_existing and name == SERVICE_STATUS_TOOL:
            if arguments:
                raise AtlasError('invalid_request', 'service status does not accept arguments')
            result = client.attached_status()
        else:
            models = MODELS if attach_existing else MCP_MODELS
            action = next((action for action in models if 'atlas_' + action.replace('.', '_') == name), None)
            if action is None:
                raise AtlasError('unsupported_action', name, 422)
            body = arguments if arguments is not None else {}
            result = client.call(parse_request(action, body, models=models))
        return json.loads(json.dumps(asdict(result), default=str))
    except AtlasError as error:
        return {'error': {'code': error.code, 'message': str(error), 'status': error.status}}


async def serve(client):
    import asyncio
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, CallToolResult

    server = Server('knowledge-atlas', version=__version__)

    @server.list_tools()
    async def list_tools():
        return [Tool(**item) for item in tools_for(attach_existing=client.attach_existing)]

    @server.call_tool(validate_input=False)
    async def invoke(name, arguments):
        result = await asyncio.to_thread(call_tool, client, name, arguments, attach_existing=client.attach_existing)
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result))],
                              structuredContent=result, isError=result.get('error') is not None)

    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        await asyncio.to_thread(client.close)
