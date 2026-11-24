"""Exercise container attachment without Docker or live deployment data."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from threading import Barrier
import time
from uuid import uuid4

import pytest

from knowledge_atlas import __version__
from knowledge_atlas.contracts import ActionRequest, ActionResult, AtlasError, ServiceContext
from knowledge_atlas.http_api import create_app
from knowledge_atlas.mcp_bridge import SessionClient, call_tool


FIRST_ID = 'a' * 64
SECOND_ID = 'b' * 64
NAME = 'atlas-exact'
REMOTE_PYTHON = '/opt/knowledge-atlas/.venv/bin/python'


def connect(session, name=NAME):
    return call_tool(session, 'atlas_atlas_connect', {'payload': {'container_name': name}})


def call(session, action, **request):
    return call_tool(session, 'atlas_' + action.replace('.', '_'), request)


@pytest.fixture
def services(tmp_path):
    apps = []

    def start(label):
        app = create_app(ServiceContext(tmp_path / label / 'service'))
        app.start()
        apps.append(app)
        config = tmp_path / (label + '.json')
        config.write_text(json.dumps({'service_root': str(app.context.service_root)}))
        return app, config

    yield start
    for app in apps:
        if app.server is not None:
            app.stop(wait=True)


@pytest.fixture
def docker(tmp_path, monkeypatch):
    targets = tmp_path / 'targets.json'
    targets.write_text('{}')
    log = tmp_path / 'docker.jsonl'
    fake = tmp_path / 'docker'
    fake.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ['ATLAS_TEST_DOCKER_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
targets = json.loads(Path(os.environ['ATLAS_TEST_DOCKER_TARGETS']).read_text())
if args[:3] == ['container', 'inspect', '--format'] and args[-2] == '--':
    if args[-1] not in targets:
        sys.exit(1)
    print(json.dumps(targets[args[-1]]['inspect']))
elif args[:2] == ['exec', '-i']:
    target = next((entry for entry in targets.values() if entry['inspect']['id'] == args[2]), None)
    if target is None or args[3] != '/opt/knowledge-atlas/.venv/bin/python':
        sys.exit(1)
    os.environ['KNOWLEDGE_ATLAS_CONFIG'] = target['config']
    if target.get('bridge'):
        os.execv(sys.executable, [sys.executable, target['bridge']])
    os.execv(sys.executable, [sys.executable, *args[4:]])
else:
    sys.exit(2)
''')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('ATLAS_TEST_DOCKER_LOG', str(log))
    monkeypatch.setenv('ATLAS_TEST_DOCKER_TARGETS', str(targets))

    def add(name, identifier, config, *, running=True, actual_name=None, bridge=None):
        state = json.loads(targets.read_text())
        state[name] = {'inspect': {'id': identifier, 'name': '/' + (actual_name or name), 'running': running},
                       'config': str(config), 'bridge': str(bridge) if bridge else None}
        targets.write_text(json.dumps(state))

    def commands():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return add, commands


@pytest.fixture
def session(tmp_path):
    config = tmp_path / 'unused-native.json'
    config.write_text(json.dumps({'service_root': str(tmp_path / 'unused-native')}))
    client = SessionClient(config, 'mcp')
    yield client
    client.close()
    assert not (tmp_path / 'unused-native').exists()


@pytest.mark.parametrize('failure', ['identity', 'api_version', 'bridge_version', 'initialize_only', 'no_service'])
def test_attach_requires_authenticated_matching_backend(
        tmp_path, docker, services, session, monkeypatch, failure):
    from knowledge_atlas import http_api

    app, config = services('first')
    add, commands = docker
    backend_calls = []
    original_invoke = http_api.invoke

    def record(context, request):
        backend_calls.append(request.action)
        return original_invoke(context, request)

    monkeypatch.setattr(http_api, 'invoke', record)
    bridge = None
    if failure == 'identity':
        (app.context.service_root / 'token').write_text('unverified-fixture-token')
    elif failure == 'api_version':
        monkeypatch.setattr(http_api, '__version__', '999.0.0')
    elif failure == 'no_service':
        app.stop(wait=True)
    else:
        bridge = tmp_path / 'initialize_only.py'
        version = '999.0.0' if failure == 'bridge_version' else __version__
        bridge.write_text(f"""
import asyncio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
server = Server('knowledge-atlas', version={version!r})
async def main():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
asyncio.run(main())
""")
    add(NAME, FIRST_ID, config, bridge=bridge)
    response = connect(session)
    assert response['error']['code'] == {
        'identity': 'service_unavailable',
        'api_version': 'version_conflict',
        'bridge_version': 'version_conflict',
        'initialize_only': 'backend_unavailable',
        'no_service': 'service_unavailable',
    }[failure]
    assert backend_calls == []
    assert not (app.context.service_root / 'launch-config.json').exists()
    assert commands()[1][:4] == ['exec', '-i', FIRST_ID, REMOTE_PYTHON]


def test_first_bind_race_has_one_winner(monkeypatch, session):
    from knowledge_atlas import mcp_bridge

    started = []

    class Container:
        def __init__(self, container_name, channel):
            started.append(container_name)
            self.container_id = {NAME: FIRST_ID, 'other': SECOND_ID}[container_name]
            self.version = __version__

        def close(self):
            pass

    monkeypatch.setattr(mcp_bridge, 'ContainerClient', Container, raising=False)
    monkeypatch.setattr(mcp_bridge, 'resolve_container',
                        lambda name: {NAME: FIRST_ID, 'other': SECOND_ID}[name], raising=False)
    barrier = Barrier(2)

    def first_call(name):
        barrier.wait(timeout=5)
        return connect(session, name)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(first_call, (NAME, 'other')))

    assert len(started) == 1
    assert len([response for response in results if 'data' in response]) == 1
    failures = [response['error'] for response in results if 'error' in response]
    assert len(failures) == 1
    assert failures[0]['status'] == 409
    assert 'new MCP connection' in failures[0]['message']


def test_failed_attach_keeps_unbound(docker, services, session):
    _, config = services('ready')
    add, commands = docker
    add(NAME, FIRST_ID, config, running=False)
    add('abbreviated', FIRST_ID, config, actual_name=NAME)
    add('ready', SECOND_ID, config)
    for name in (NAME, 'absent', 'abbreviated'):
        assert connect(session, name)['error']['code'] == 'backend_unavailable'
    before = commands()
    assert len(before) == 3 and all(argv[:2] == ['container', 'inspect'] for argv in before)
    assert connect(session, 'ready')['data'] == {'container_id': SECOND_ID, 'version': __version__}


def test_same_target_connect_is_idempotent(docker, services, session):
    _, config = services('first')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    first = connect(session)
    assert first['data'] == {'container_id': FIRST_ID, 'version': __version__}
    assert connect(session) == first
    assert len([cmd for cmd in commands() if cmd[0] == 'exec']) == 1


def test_connect_recognizes_same_full_id_after_rename(docker, services, session):
    _, config = services('first')
    _, replacement = services('replacement')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    first = connect(session)
    assert first['data']['container_id'] == FIRST_ID
    add('renamed', FIRST_ID, config)
    add(NAME, SECOND_ID, replacement)
    reused = connect(session)
    assert reused['error']['status'] == 503
    assert connect(session, 'renamed') == first
    assert [argv[2] for argv in commands() if argv[0] == 'exec'] == [FIRST_ID]


def test_retarget_is_rejected(docker, services, session):
    _, config = services('first')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    add('other', SECOND_ID, config)
    assert 'data' in connect(session)
    response = connect(session, 'other')
    assert response['error']['status'] == 409
    assert 'new MCP connection' in response['error']['message']
    assert all(argv[2] == FIRST_ID for argv in commands() if argv[0] == 'exec')
    assert call(session, 'atlas.list')['data'] == {'projects': []}


def test_reused_container_name_does_not_redirect(docker, services, session):
    first, config = services('first')
    _, replacement = services('replacement')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    assert connect(session)['data']['container_id'] == FIRST_ID
    first.stop(wait=True)
    add(NAME, SECOND_ID, replacement)
    for result in (call(session, 'atlas.list'), connect(session)):
        assert result['error']['status'] == 503
    assert all(SECOND_ID not in argv for argv in commands())


def test_delayed_poll_stays_on_original_service(docker, services, session, tmp_path):
    _, config = services('first')
    _, replacement = services('replacement')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    assert 'data' in connect(session)
    operation = call(session, 'atlas.create', request_id=str(uuid4()),
                     payload={'path': str(tmp_path / 'atlas')})
    add(NAME, SECOND_ID, replacement)
    deadline = time.monotonic() + 10
    while True:
        operation = call(session, 'operation.read', payload={'operation_id': operation['operation_id']})
        assert operation.get('error') is None, operation
        if operation['state'] not in ('queued', 'running'):
            break
        assert time.monotonic() < deadline
        time.sleep(.05)
    identity = operation['result']['data']['atlas_id']
    assert call(session, 'atlas.open', atlas_id=identity)['data']['atlas_id'] == identity
    assert all(SECOND_ID not in argv for argv in commands())


def test_container_client_passes_exact_arguments_and_open_uuid(docker, services):
    from knowledge_atlas.container_client import ContainerClient

    _, config = services('first')
    add, commands = docker
    add(NAME, FIRST_ID, config)
    for name in ('--help', '/atlas', 'my-atlas; echo unsafe'):
        with pytest.raises(AtlasError, match='exact Docker container name'):
            ContainerClient(name, 'mcp')
    assert commands() == []

    remote = ContainerClient(NAME, 'mcp')
    try:
        inspect, exec_command = commands()
        assert inspect[:3] == ['container', 'inspect', '--format']
        assert inspect[-2:] == ['--', NAME]
        assert exec_command == ['exec', '-i', FIRST_ID, REMOTE_PYTHON,
                                '-m', 'knowledge_atlas.cli', 'mcp', '--attach-existing', '--channel', 'mcp']
        assert remote.call(ActionRequest('atlas.list')) == ActionResult(data={'projects': []})
        with pytest.raises(AtlasError, match='atlas_not_found'):
            remote.call(ActionRequest('atlas.open', atlas_id=str(uuid4())))
        assert commands() == [inspect, exec_command]
    finally:
        remote.close()


def test_viewer_launch_opens_container_path_through_exact_transport(docker, services, tmp_path, monkeypatch):
    from knowledge_atlas.viewer import backend, server
    from test_project_registry import create_project

    app, config = services('viewer')
    atlas = tmp_path / 'existing atlas'
    identity = create_project(app.context, atlas)
    add, commands = docker
    add(NAME, FIRST_ID, config)
    seen = []

    class Viewer:
        launch_url = 'http://localhost/viewer-test'

        def __init__(self, connection, atlas_id, port):
            assert connection.container_id == FIRST_ID
            assert atlas_id == identity
            assert port == 12345

        def serve_forever(self):
            seen.append('served')

        def server_close(self):
            seen.append('closed')

    monkeypatch.setattr(server, 'ViewerServer', Viewer)
    backend.run_viewer(str(atlas), container_name=NAME, port=12345)
    assert seen == ['served', 'closed']
    assert [argv[:4] for argv in commands()[1:]] == [
        ['exec', '-i', FIRST_ID, REMOTE_PYTHON],
        ['exec', '-i', FIRST_ID, REMOTE_PYTHON],
    ]
    assert commands()[-1][-1] == str(atlas)


@pytest.mark.parametrize('payload', [{}, {'container_name': 42},
                                     {'container_name': NAME, 'project': 'old'}])
def test_connect_rejects_invalid_payload_before_container_resolution(session, docker, payload):
    _, commands = docker
    assert call(session, 'atlas.connect', payload=payload)['error']['code'] == 'invalid_request'
    assert commands() == []


def test_pinned_transport_reports_unavailable_if_loop_closes_during_call():
    import asyncio
    from threading import Event
    from types import SimpleNamespace

    from knowledge_atlas.viewer.backend import MCPBackend

    async def call_tool(*_):
        pytest.fail('a closed transport cannot send an action')

    backend = object.__new__(MCPBackend)
    backend.error = None
    backend.stopped = Event()
    backend.thread = SimpleNamespace(is_alive=lambda: True)
    backend.loop = asyncio.new_event_loop()
    backend.session = SimpleNamespace(call_tool=call_tool)
    backend.loop.close()
    with pytest.raises(AtlasError, match='backend_unavailable'):
        backend.call('operation.read', {'payload': {'operation_id': 'original'}})
