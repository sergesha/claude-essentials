"""Persistent stdio transport for the viewer and exact-container attachment."""
import asyncio
from datetime import timedelta
import os
import re
import sys
import threading

from .. import __version__
from ..contracts import AtlasError
from ..project_registry import project_id


CONTAINER_ID = re.compile(r'[0-9a-f]{64}')
SERVICE_STATUS_TOOL = 'atlas_service_status'


def container_python(container_id):
    if not CONTAINER_ID.fullmatch(container_id):
        raise AtlasError('invalid_request', 'a full Docker container ID is required')
    return ['docker', 'exec', '-i', container_id, '/opt/knowledge-atlas/.venv/bin/python']


def _tool_result(result):
    value = result.structuredContent
    if not isinstance(value, dict) or (result.isError and 'error' not in value):
        raise AtlasError('backend_unavailable', 'invalid MCP response from the Atlas service', 503)
    return value


def _ready_version(result):
    if 'error' in result:
        error = result['error']
        raise AtlasError(error['code'], error['message'], error['status'])
    data = result.get('data')
    version = data.get('version') if isinstance(data, dict) else None
    if version is None:
        raise AtlasError('backend_unavailable', 'Atlas service did not report authenticated readiness', 503)
    if version != __version__:
        raise AtlasError('version_conflict', 'the plugin and the running Atlas service must use matching versions', 409)
    return version


class MCPBackend:
    def __init__(self, config=None, *, container_id=None, channel='viewer'):
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.version = None
        self.config = config
        self.container_id = container_id
        self.channel = channel
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self.ready.wait(60):
            self.close()
            raise AtlasError('backend_unavailable', 'MCP attachment timed out', 503)
        if self.error is not None:
            if isinstance(self.error, AtlasError):
                raise self.error
            raise AtlasError('backend_unavailable', 'cannot attach to the Atlas MCP backend', 503) from self.error

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.loop = asyncio.get_running_loop()
        if self.container_id:
            command, *args = container_python(self.container_id)
            args += ['-m', 'knowledge_atlas.cli', 'mcp', '--attach-existing']
        else:
            command = sys.executable
            args = ['-m', 'knowledge_atlas.cli', 'mcp']
            if self.config:
                args += ['--config', str(self.config)]
        args += ['--channel', self.channel]
        params = StdioServerParameters(command=command, args=args, env=dict(os.environ))
        async with stdio_client(params, errlog=sys.__stderr__) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=900)) as session:
                try:
                    async with asyncio.timeout(45):
                        initialized = await session.initialize()
                        if self.container_id:
                            info = initialized.serverInfo
                            if info.name != 'knowledge-atlas':
                                raise AtlasError('backend_unavailable', 'expected a Knowledge Atlas MCP backend', 503)
                            if info.version != __version__:
                                raise AtlasError('version_conflict', 'the plugin and the container must use matching Atlas versions', 409)
                            status = await session.call_tool(SERVICE_STATUS_TOOL, {})
                            self.version = _ready_version(_tool_result(status))
                except Exception as error:
                    self.error = error
                    return
                self.session = session
                self.ready.set()
                while not self.stopped.is_set():
                    await asyncio.sleep(.1)

    def _call(self, name, request, *, timeout=910):
        if self.error is not None or self.stopped.is_set() or not self.thread.is_alive():
            raise AtlasError('backend_unavailable', 'Atlas MCP session stopped', 503)
        pending = self.session.call_tool(name, request)
        try:
            future = asyncio.run_coroutine_threadsafe(pending, self.loop)
        except RuntimeError as error:
            pending.close()
            raise AtlasError('backend_unavailable', 'Atlas MCP session stopped', 503) from error
        try:
            result = future.result(timeout=timeout)
        except Exception as error:
            future.cancel()
            raise AtlasError('backend_unavailable', 'Atlas MCP session did not return a response', 503) from error
        return _tool_result(result)

    def call(self, action, request):
        return self._call('atlas_' + action.replace('.', '_'), request)

    def verify_ready(self):
        return _ready_version(self._call(SERVICE_STATUS_TOOL, {}, timeout=15))

    def verify_atlas(self, identity):
        response = self.call('atlas.open', {'atlas_id': identity})
        if 'error' in response:
            error = response['error']
            raise AtlasError(error['code'], error['message'], error['status'])
        if response.get('data', {}).get('atlas_id') != identity:
            raise AtlasError('backend_unavailable', 'service returned a different atlas identity', 503)
        return response

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)


def run_viewer(atlas, config=None, *, container_name=None, port=0):
    from ..container_client import resolve_container
    from ..launcher import reject_removed_deployment_settings
    from .server import ViewerServer

    atlas_id = project_id(atlas)
    reject_removed_deployment_settings()
    container_id = resolve_container(container_name) if container_name is not None else None
    backend = MCPBackend(config, container_id=container_id)
    try:
        backend.verify_atlas(atlas_id)
        server = ViewerServer(backend, atlas_id, port)
        print('Knowledge Atlas viewer: ' + server.launch_url, flush=True)
        print('Local session only. Relaunch creates a new browser secret.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        backend.close()
