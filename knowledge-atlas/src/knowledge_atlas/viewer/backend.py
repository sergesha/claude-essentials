"""Persistent MCP attachment owned by the local viewer process."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from ..contracts import AtlasError
from ..project_registry import repository_identity
from ..runtime import _assets


def compose_python(project):
    compose = _assets().parent / 'docker/compose.yaml'
    return ['docker', 'compose', '-p', project, '-f', str(compose), 'exec', '-T',
            'atlas', '/opt/knowledge-atlas/.venv/bin/python']


class MCPBackend:
    def __init__(self,config=None,docker_project=None,*,channel='viewer'):
        self.ready=threading.Event();self.stopped=threading.Event();self.error=None
        self.config=config;self.docker_project=docker_project;self.channel=channel
        self.thread=threading.Thread(target=self._thread,daemon=True)
        self.thread.start()
        if not self.ready.wait(60):
            self.close();raise AtlasError('backend_unavailable','MCP attachment timed out',503)
        if self.error:raise AtlasError('backend_unavailable',str(self.error),503)

    def _thread(self):
        try:asyncio.run(self._serve())
        except Exception as error:self.error=error;self.ready.set()

    async def _serve(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        self.loop=asyncio.get_running_loop()
        if self.docker_project:
            command,*args=compose_python(self.docker_project)
            args+=['-m','knowledge_atlas.cli','mcp']
        else:
            command=sys.executable;args=['-m','knowledge_atlas.cli','mcp']
            if self.config:args+=['--config',str(self.config)]
        args += ['--channel',self.channel]
        async with stdio_client(StdioServerParameters(command=command,args=args,env=dict(os.environ))) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=900)) as session:
                await session.initialize();self.session=session;self.ready.set()
                while not self.stopped.is_set():await asyncio.sleep(.1)

    def call(self,action,request):
        if self.error or not self.thread.is_alive():raise AtlasError('backend_unavailable','MCP session stopped',503)
        future=asyncio.run_coroutine_threadsafe(self.session.call_tool('atlas_'+action.replace('.','_'),request),self.loop)
        result=future.result(timeout=910)
        value=result.structuredContent
        if value is None:
            value=json.loads(next(item.text for item in result.content if item.type=='text'))
        return value

    def open_atlas(self, selection):
        try:
            identity = str(uuid.UUID(str(selection)))
        except ValueError:
            if self.docker_project:
                command = ("from pathlib import Path; import sys; "
                           "from knowledge_atlas.project_registry import repository_identity; "
                           "print(repository_identity(Path(sys.argv[1]).expanduser()))")
                try:
                    resolved = subprocess.run([*compose_python(self.docker_project), '-c', command, str(selection)],
                                              capture_output=True, text=True, check=True, timeout=30)
                    identity = str(uuid.UUID(resolved.stdout.strip()))
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    raise AtlasError('backend_unavailable', 'cannot resolve the atlas path in the selected container', 503) from error
            else:
                identity = repository_identity(Path(selection).expanduser())
        response = self.call('atlas.open', {'atlas_id': identity})
        if 'error' in response:
            error = response['error']
            raise AtlasError(error['code'], error['message'], error['status'])
        if response.get('data', {}).get('atlas_id') != identity:
            raise AtlasError('backend_unavailable', 'service returned a different atlas identity', 503)
        return identity

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():self.thread.join(timeout=10)


def run_viewer(atlas,config=None,docker_project=None,port=0):
    from .server import ViewerServer
    backend=MCPBackend(config,docker_project)
    try:
        atlas_id=backend.open_atlas(atlas)
        server=ViewerServer(backend,atlas_id,port)
        print('Knowledge Atlas viewer: '+server.launch_url,flush=True)
        print('Local session only. Relaunch creates a new browser secret.',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()
    finally:backend.close()
