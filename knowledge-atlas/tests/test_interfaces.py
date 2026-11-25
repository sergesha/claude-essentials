"""Real localhost transport parity, authentication, and durable operations."""
from dataclasses import asdict
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid
import pytest
from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext


@pytest.fixture
def app(tmp_path):
    from knowledge_atlas.http_api import create_app
    app=create_app(ServiceContext(tmp_path/'service'))
    app.start()
    yield app
    app.stop(wait=True)


def raw(app,action,payload,headers=None):
    request=urllib.request.Request(app.endpoint+'/v1/actions/'+action,data=json.dumps(payload).encode(),
        headers=headers or {'Authorization':'Bearer '+app.token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as response:
        return response.status,json.load(response)


def test_three_interfaces_preserve_results_and_errors(app,tmp_path):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.mcp_bridge import call_tool
    client=AtlasClient(app.context.service_root)
    request=ActionRequest('atlas.create',request_id=str(uuid.uuid4()),payload={'path':str(tmp_path/'atlas')})
    status,submitted=raw(app,'atlas.create',asdict(request))
    assert status==202
    from knowledge_atlas.api_models import Location
    repeated=client.atlas_create(Location(path=str(tmp_path/'atlas')),request_id=request.request_id)
    assert repeated.operation_id==submitted['operation_id']
    deadline=time.monotonic()+15
    while True:
        operation=client.operation(submitted['operation_id'])
        if operation.state not in ('queued','running'):break
        assert time.monotonic()<deadline
        time.sleep(.05)
    assert operation.state=='succeeded',operation.error
    atlas_id=operation.result.data['atlas_id']
    second=client.atlas_create(Location(path=str(tmp_path/'second')),request_id=str(uuid.uuid4()))
    deadline=time.monotonic()+15
    while second.state in ('queued','running'):
        assert time.monotonic()<deadline
        time.sleep(.05)
        second=client.operation(second.operation_id)
    assert second.state=='succeeded',second.error
    second_id=second.result.data['atlas_id']

    status,http=raw(app,'atlas.list',{})
    assert status==200 and http==asdict(client.atlas_list())==call_tool(client,'atlas_atlas_list',None)
    assert {row['atlas_id'] for row in http['data']['projects']}=={atlas_id,second_id}
    for identity in (atlas_id,second_id,atlas_id):
        selection={'atlas_id':identity}
        status,http=raw(app,'atlas.open',selection)
        assert status==200 and http==asdict(client.atlas_open(**selection))
        assert http==call_tool(client,'atlas_atlas_open',selection)
        assert http['data']['atlas_id']==identity

    request=ActionRequest('atlas.status',atlas_id=atlas_id)
    status,http=raw(app,request.action,asdict(request))
    python=asdict(client.call(request))
    mcp=call_tool(client,'atlas_atlas_status',asdict(request))
    assert status==200 and http==python==mcp
    bad=ActionRequest('atlas.status',atlas_id=str(uuid.uuid4()))
    with pytest.raises(AtlasError) as python_error:client.call(bad)
    mcp_error=call_tool(client,'atlas_atlas_status',asdict(bad))
    assert python_error.value.code==mcp_error['error']['code']=='atlas_not_found'
    assert python_error.value.status==mcp_error['error']['status']==404


def test_unauthenticated_request_never_dispatches(app,tmp_path):
    from knowledge_atlas.operations import database
    payload=asdict(ActionRequest('atlas.create',request_id=str(uuid.uuid4()),payload={'path':str(tmp_path/'forbidden')}))
    for headers,status in [({'Content-Type':'application/json'},401),
            ({'Authorization':'Bearer '+app.token,'Host':'evil.example'},400),
            ({'Authorization':'Bearer '+app.token,'Origin':'https://evil.example'},400)]:
        with pytest.raises(urllib.error.HTTPError) as error:raw(app,'atlas.create',payload,headers)
        assert error.value.code==status
    with database(app.context) as db:assert db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert not (tmp_path/'forbidden').exists()
    assert (app.context.service_root/'token').stat().st_mode & 0o777 == 0o600
    assert (app.context.service_root/'discovery.json').stat().st_mode & 0o777 == 0o600


def test_schema_is_shared_with_mcp(app):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.mcp_bridge import tools_for
    client=AtlasClient(app.context.service_root)
    schema=client.schema()
    tools=tools_for()
    domain_tools = {tool['name']: tool for tool in tools if tool['name'] != 'atlas_atlas_connect'}
    assert set(domain_tools) == {'atlas_' + name.replace('.', '_') for name in schema['actions']}
    for name, action in schema['actions'].items():
        assert domain_tools['atlas_' + name.replace('.', '_')]['inputSchema'] == action['input']
    assert 'packages.pin' not in schema['actions']
    assert 'atlas_packages_pin' not in domain_tools
    assert not hasattr(client, 'packages_pin')
    assert 'atlas.connect' not in schema['actions']
    connect = next(tool['inputSchema'] for tool in tools if tool['name'] == 'atlas_atlas_connect')
    assert connect['required'] == ['payload']
    assert set(connect['$defs']['ContainerLocation']['properties']) == {'container_name'}


def test_mcp_opens_registered_uuid_without_configured_path(app, tmp_path):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.mcp_bridge import SessionClient, call_tool
    client = AtlasClient(app.context.service_root)
    path = tmp_path/'bound'
    created = client.call(ActionRequest('atlas.create', request_id=str(uuid.uuid4()), payload={'path':str(path)}))
    deadline = time.monotonic() + 15
    while (record := client.operation(created.operation_id)).state in ('queued', 'running'):
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert record.state == 'succeeded', record.error
    identity = record.result.data['atlas_id']
    config = tmp_path/'service.json'
    config.write_text(json.dumps({'service_root': str(app.context.service_root)}))
    session = SessionClient(config, 'mcp')
    assert call_tool(session, 'atlas_atlas_open', {'atlas_id': identity})['data']['atlas_id'] == identity


def test_native_list_without_docker(tmp_path, monkeypatch):
    import os
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.mcp_bridge import SessionClient, call_tool

    root = tmp_path / 'native-service'
    config = tmp_path / 'native.json'
    config.write_text(json.dumps({'service_root': str(root)}))
    docker = tmp_path / 'docker'
    called = tmp_path / 'docker-called'
    docker.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 1\n')
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('KNOWLEDGE_ATLAS_DEFAULT_PROJECT', 'saved-deployment')
    session = SessionClient(config, 'mcp')
    assert not root.exists()
    try:
        assert call_tool(session, 'atlas_atlas_list', None)['data'] == {'projects': []}
        response = call_tool(session, 'atlas_atlas_connect', {'payload': {'container_name': 'other'}})
        assert response['error']['status'] == 409
        assert 'new MCP connection' in response['error']['message']
        assert not called.exists()
    finally:
        if (root / 'discovery.json').exists():
            AtlasClient(root).stop()
        session.close()


def test_removed_compose_settings_are_actionable(tmp_path, monkeypatch, capsys):
    import sys
    from knowledge_atlas import cli, mcp_bridge

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('KNOWLEDGE_ATLAS_DEFAULT_PROJECT', 'saved-in-claude')
    monkeypatch.setenv('KNOWLEDGE_ATLAS_DOCKER_PROJECT', 'saved-in-env')
    saved = tmp_path / 'knowledge-atlas/mcp.json'
    saved.parent.mkdir()
    stale_settings = '{"project":"saved-on-disk"}'
    saved.write_text(stale_settings)

    async def run(client):
        arguments = {'atlas_id': str(uuid.uuid4()), 'payload': {'project': 'saved-on-disk'}}
        response = mcp_bridge.call_tool(client, 'atlas_atlas_open', arguments)
        assert response['error']['code'] == 'invalid_request'
        assert 'atlas.connect' in response['error']['message']
        assert 'container_name' in response['error']['message']

    monkeypatch.setattr(mcp_bridge, 'serve', run)
    monkeypatch.setattr(sys, 'argv', ['knowledge-atlas', 'mcp'])
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert 'KNOWLEDGE_ATLAS_DEFAULT_PROJECT' in captured.err
    assert 'KNOWLEDGE_ATLAS_DOCKER_PROJECT' in captured.err
    assert 'mcp.json' in captured.err
    assert 'atlas.connect' in captured.err and 'container_name' in captured.err
    assert saved.read_text() == stale_settings


def test_native_mcp_session_does_not_follow_rewritten_discovery(app, tmp_path):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.http_api import create_app
    from knowledge_atlas.mcp_bridge import SessionClient, call_tool

    other = create_app(ServiceContext(tmp_path / 'other'))
    other.start()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'service_root': str(app.context.service_root)}))
    session = SessionClient(config, 'mcp')
    try:
        assert call_tool(session, 'atlas_atlas_list', None)['data'] == {'projects': []}
        operation = AtlasClient(other.context.service_root).call(ActionRequest(
            'atlas.create', request_id=str(uuid.uuid4()), payload={'path': str(tmp_path / 'other-atlas')}))
        assert operation.operation_id
        discovery = app.context.service_root / 'discovery.json'
        token = app.context.service_root / 'token'
        discovery.write_bytes((other.context.service_root / 'discovery.json').read_bytes())
        token.write_bytes((other.context.service_root / 'token').read_bytes())
        poll = call_tool(session, 'atlas_operation_read', {'payload': {'operation_id': operation.operation_id}})
        assert poll['error']['code'] == 'operation_not_found'
    finally:
        session.close()
        other.stop(wait=True)


def test_singleton_launcher_and_version_conflict(tmp_path):
    from knowledge_atlas.launcher import ensure_service
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.locks import is_locked
    from knowledge_atlas.mcp_bridge import SessionClient, call_tool
    config={'service_root':str(tmp_path/'service')}
    config_path = tmp_path/'config.json'
    config_path.write_text(json.dumps(config))
    first=ensure_service(config)
    try:
        second=ensure_service(config)
        assert first['pid']==second['pid'] and first['endpoint']==second['endpoint']
        client=AtlasClient(config['service_root'])
        path=Path(config['service_root'])/'discovery.json'
        actual=path.read_text();changed=json.loads(actual);changed['version']='999.0.0';path.write_text(json.dumps(changed))
        try:
            with pytest.raises(AtlasError,match='version_conflict'):ensure_service(config)
            session = SessionClient(config_path, 'mcp')
            missing = call_tool(session, 'atlas_atlas_open', {})['error']
            assert (missing['code'], missing['status']) == ('invalid_request', 400)
            explicit = call_tool(session, 'atlas_atlas_open', {'atlas_id': str(uuid.uuid4())})
            assert explicit['error']['code'] == 'version_conflict'
        finally:path.write_text(actual)
        client.stop()
        deadline=time.monotonic()+10
        while is_locked(Path(config['service_root'])/'service.lock'):
            assert time.monotonic()<deadline
            time.sleep(.05)
    finally:
        if is_locked(Path(config['service_root'])/'service.lock'):AtlasClient(config['service_root']).stop()


def test_mcp_sdk_discovers_tools_and_calls_same_service(tmp_path):
    import asyncio,sys
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from knowledge_atlas.launcher import ensure_service
    from knowledge_atlas.client import AtlasClient
    config={'service_root':str(tmp_path/'service')}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    ensure_service(config)
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-m','knowledge_atlas.cli','mcp','--config',str(path)])
        async with stdio_client(params, errlog=sys.__stderr__) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                listed=await session.list_tools()
                assert any(t.name=='atlas_atlas_create' for t in listed.tools)
                response=await session.call_tool('atlas_atlas_status',{'atlas_id':str(uuid.uuid4()),'payload':{}})
                assert response.isError
                assert response.structuredContent['error']['code']=='atlas_not_found'
                accepted=await session.call_tool('atlas_atlas_create',{'request_id':str(uuid.uuid4()),'payload':{'path':str(tmp_path/'atlas')}})
                assert not accepted.isError, accepted.structuredContent
                operation=accepted.structuredContent
                while operation['state'] in ('queued','running'):
                    await asyncio.sleep(.05)
                    polled=await session.call_tool('atlas_operation_read',{'payload':{'operation_id':operation['operation_id']}})
                    assert not polled.isError, polled.structuredContent
                    operation=polled.structuredContent
                assert operation['state']=='succeeded', operation
    try:asyncio.run(run())
    finally:AtlasClient(config['service_root']).stop()


def test_schema_covers_results_and_rejects_coerced_inputs(app):
    from knowledge_atlas.client import AtlasClient
    client=AtlasClient(app.context.service_root)
    outputs=client.schema()['results']
    assert {'data','publication','evidence','canonical_effects'} <= set(outputs['ActionResult']['properties'])
    assert 'operation_id' in outputs['OperationRecord']['properties']
    with pytest.raises(urllib.error.HTTPError) as error:
        raw(app,'search',{'atlas_id':'missing','payload':{'query':'x','count':True}})
    assert error.value.code==400


def test_stop_closes_mutation_admission(app,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.operations import database
    entered=threading.Event();release=threading.Event()
    original=app.dispatcher.stop
    def paused_stop(*,wait=False):
        original(wait=wait)
        if not entered.is_set():
            entered.set();assert release.wait(5)
    monkeypatch.setattr(app.dispatcher,'stop',paused_stop)
    with ThreadPoolExecutor(2) as pool:
        stopping=pool.submit(AtlasClient(app.context.service_root).stop)
        assert entered.wait(5)
        request=ActionRequest('atlas.create',request_id=str(uuid.uuid4()),payload={'path':str(tmp_path/'late')})
        mutation=pool.submit(raw,app,'atlas.create',asdict(request))
        time.sleep(.1);release.set()
        stopping.result(timeout=5)
        with pytest.raises((urllib.error.HTTPError,urllib.error.URLError,ConnectionError)) as error:mutation.result(timeout=5)
        if isinstance(error.value,urllib.error.HTTPError):assert error.value.code==409
    with database(app.context) as db:assert db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_schema_does_not_accept_discarded_fields():
    from knowledge_atlas.api_models import parse_request
    samples=[('candidate.decide',{'candidate':'a'*40,'decision_id':str(uuid.uuid4()),'action':'reject',
        'actor':'Fixture','kind':'synthetic','reason':'explicit','decided_at':'2026-11-13T00:00:00Z'}),
        ('feedback.record',{'record_id':'r','applied_publication':'a'*40,'raw_report':'{}','context':{'outcome':'ignored'},
                            'actor':'Fixture','recorded_at':'2026-11-13T00:00:00Z'})]
    for action,payload in samples:
        with pytest.raises(AtlasError,match='invalid_request'):
            parse_request(action,{'request_id':str(uuid.uuid4()),'payload':payload})


def test_client_rediscovers_restarted_service_and_rotated_token(tmp_path):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.http_api import create_app
    root = tmp_path / 'service'
    first = create_app(ServiceContext(root)).start()
    client = AtlasClient(root)
    try:
        original_token = first.token
        assert client.request('/v1/status')['endpoint'] == first.endpoint
    finally:
        first.stop(wait=True)
    assert not (root / 'discovery.json').exists()
    second = create_app(ServiceContext(root)).start()
    try:
        assert second.token != original_token
        assert client.request('/v1/status')['endpoint'] == second.endpoint
    finally:
        second.stop(wait=True)


def test_concurrent_status_reads_use_the_live_service(app):
    from concurrent.futures import ThreadPoolExecutor
    from knowledge_atlas.client import AtlasClient
    client = AtlasClient(app.context.service_root)
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda _: client.request('/v1/status'), range(64)))
    assert all(result['endpoint'] == app.endpoint for result in results)


def test_read_provider_error_is_explicit_over_http(app, monkeypatch):
    from knowledge_atlas import http_api
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.providers.base import ProviderError
    def unavailable(context, request):
        raise ProviderError('provider_unavailable', 'private provider diagnostics')
    monkeypatch.setattr(http_api, 'invoke', unavailable)
    with pytest.raises(AtlasError) as failure:
        AtlasClient(app.context.service_root).call(ActionRequest(
            'question.ask', atlas_id='fixture', payload={'question':'What is recorded?'}))
    assert failure.value.code == 'provider_failed'
    assert failure.value.status == 503
    assert 'private provider diagnostics' not in str(failure.value)


def test_status_marks_closed_http11_connections(app):
    from http.client import HTTPConnection
    connection = HTTPConnection('127.0.0.1', app.server.server_port)
    try:
        for _ in range(2):
            connection.request('GET', '/v1/status', headers={'Authorization':'Bearer '+app.token})
            response = connection.getresponse()
            assert response.getheader('Connection') == 'close'
            assert json.load(response)['endpoint'] == app.endpoint
    finally:
        connection.close()


@pytest.mark.parametrize('source', ['http', 'direct'])
def test_idle_dispatch_sources(tmp_path, source):
    import threading
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.operations import inspect_operation, submit
    from knowledge_atlas.http_api import create_app
    app = create_app(ServiceContext(tmp_path / 'service'))
    idle = threading.Event()
    wait = app.dispatcher.wakeup.wait

    # For HTTP, only an admission notification can wake the parked dispatcher
    # before our deadline. Direct submissions rely on its regular timer.
    def parked(timeout):
        idle.set()
        return wait(30 if source == 'http' else timeout)

    app.dispatcher.wakeup.wait = parked
    app.start()
    try:
        assert idle.wait(5)
        request = ActionRequest('atlas.create', request_id=str(uuid.uuid4()),
                                payload={'path': str(tmp_path / 'project')})
        if source == 'http':
            record = AtlasClient(app.context.service_root).call(request)
        else:
            record = submit(app.context, request)
        deadline = time.monotonic() + 8
        while record.state in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.1)
            record = inspect_operation(app.context, record.operation_id)
        assert record.state == 'succeeded', record
    finally:
        app.stop(wait=True)
