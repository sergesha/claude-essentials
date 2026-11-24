"""A project-scoped connection to the MCP server in an existing atlas container."""
from dataclasses import asdict
from functools import cached_property
import json
import subprocess

from .client import AtlasClient
from .contracts import AtlasError
from .viewer.backend import MCPBackend, compose_python


class ComposeClient:
    def __init__(self, project, channel):
        self.project = project
        try:
            self.connection = MCPBackend(docker_project=project, channel=channel)
        except AtlasError as error:
            raise AtlasError('backend_unavailable', f'cannot connect to atlas in Compose project {project}', 503) from error

    @cached_property
    def bound_path(self):
        # Older containers require a path even for a bound deployment. Read only
        # this one setting from the same instance, once per MCP connection.
        command = ("import json; from knowledge_atlas.launcher import load_config; "
                   "print(json.dumps(load_config().get('configuration', {}).get('atlas_path')))")
        try:
            run = subprocess.run([*compose_python(self.project), '-c', command], check=True,
                                 capture_output=True, text=True, timeout=30)
            path = json.loads(run.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AtlasError('backend_unavailable', f'cannot read atlas path for Compose project {self.project}', 503) from error
        if not isinstance(path, str) or not path:
            raise AtlasError('invalid_request', 'this service has no configured atlas_path; supply a path to open')
        return path

    def call(self, request):
        body = asdict(request)
        if request.action == 'atlas.open' and body['payload'].get('path') is None:
            body['payload']['path'] = self.bound_path
        try:
            result = self.connection.call(request.action, body)
        except Exception as error:
            raise AtlasError('backend_unavailable', f'MCP connection to Compose project {self.project} failed', 503) from error
        if 'error' in result and 'operation_id' not in result:
            error = result['error']
            raise AtlasError(error['code'], error['message'], error['status'])
        return AtlasClient.decode(result)

    def close(self):
        self.connection.close()
