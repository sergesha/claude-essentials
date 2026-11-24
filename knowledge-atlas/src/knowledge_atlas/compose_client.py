"""A project-scoped connection to the MCP server in an existing atlas container."""
from dataclasses import asdict

from .client import AtlasClient
from .contracts import AtlasError
from .viewer.backend import MCPBackend


class ComposeClient:
    def __init__(self, project, channel):
        self.project = project
        try:
            self.connection = MCPBackend(docker_project=project, channel=channel)
        except AtlasError as error:
            raise AtlasError('backend_unavailable', f'cannot connect to atlas in Compose project {project}', 503) from error

    def call(self, request):
        body = asdict(request)
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
