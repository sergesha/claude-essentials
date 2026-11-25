"""Attach to the Atlas API in an existing, exactly named Docker container."""
from dataclasses import asdict
import json
import re
import subprocess

from .client import AtlasClient
from .contracts import ActionRequest, ActionResult, AtlasError, OperationRecord
from .viewer.backend import CONTAINER_ID, MCPBackend


_INSPECT_FORMAT = '{"id":{{json .Id}},"name":{{json .Name}},"running":{{json .State.Running}}}'


def resolve_container(container_name: str) -> str:
    if not isinstance(container_name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', container_name):
        raise AtlasError('invalid_request', 'provide an exact Docker container name')
    try:
        inspected = subprocess.run(
            ['docker', 'container', 'inspect', '--format', _INSPECT_FORMAT, '--', container_name],
            capture_output=True, text=True, check=True, timeout=10,
        )
        target = json.loads(inspected.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise AtlasError('backend_unavailable', 'check Docker and the supplied exact container name', 503) from error
    if (not isinstance(target, dict) or target.get('name') != '/' + container_name
            or target.get('running') is not True or not isinstance(target.get('id'), str)
            or not CONTAINER_ID.fullmatch(target['id'])):
        raise AtlasError('backend_unavailable', 'the exact Docker container must exist and be running', 503)
    return target['id']


class ContainerClient:
    def __init__(self, container_name: str, channel: str):
        self.container_id = resolve_container(container_name)
        self.connection = MCPBackend(container_id=self.container_id, channel=channel)
        self.version = self.connection.version

    def verify_ready(self) -> str:
        return self.connection.verify_ready()

    def call(self, request: ActionRequest) -> ActionResult | OperationRecord:
        result = self.connection.call(request.action, asdict(request))
        if 'error' in result and 'operation_id' not in result:
            error = result['error']
            raise AtlasError(error['code'], error['message'], error['status'])
        return AtlasClient.decode(result)

    def close(self):
        self.connection.close()
