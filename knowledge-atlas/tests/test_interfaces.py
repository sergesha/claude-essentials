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
    request=ActionRequest('atlas.status',atlas_id=atlas_id)
    status,http=raw(app,request.action,asdict(request))
    python=asdict(client.call(request))
    mcp=call_tool(client,'atlas_atlas_status',asdict(request))
    assert status==200 and http==python==mcp
    bad=ActionRequest('atlas.status',atlas_id='missing')
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
    assert len(tools)==len(schema['actions'])
    assert {tool['name'] for tool in tools}=={'atlas_'+name.replace('.','_') for name in schema['actions']}
    assert all(tool['inputSchema']==schema['actions'][tool['name'][6:].replace('_','.')]['input']
               for tool in tools if tool['name'] != 'atlas_atlas_open')
    open_schema = next(tool['inputSchema'] for tool in tools if tool['name'] == 'atlas_atlas_open')
    assert 'payload' not in open_schema.get('required', [])
    assert 'project' in open_schema['$defs']['ProjectLocation']['properties']


def test_open_uses_bound_atlas_without_payload(app, tmp_path):
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.mcp_bridge import SessionClient, call_tool
    client = AtlasClient(app.context.service_root)
    path = tmp_path/'bound'
    app.context.configuration['atlas_path'] = str(path)
    created = client.call(ActionRequest('atlas.create', request_id=str(uuid.uuid4()), payload={'path':str(path)}))
    deadline = time.monotonic() + 15
    while (record := client.operation(created.operation_id)).state in ('queued', 'running'):
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert record.state == 'succeeded', record.error
    identity = record.result.data['atlas_id']
    assert client.request('/v1/actions/atlas.open', {})['data']['atlas_id'] == identity
    assert client.atlas_open().data['path'] == str(path)
    config = tmp_path/'bound.json'
    config.write_text(json.dumps({'service_root': str(app.context.service_root),
                                  'configuration': {'atlas_path': str(path)}}))
    session = SessionClient(config, 'mcp')
    assert call_tool(session, 'atlas_atlas_open', {})['data']['atlas_id'] == identity


def test_mcp_project_routing_is_per_session_and_switches_only_on_success(monkeypatch):
    from knowledge_atlas import mcp_bridge
    from knowledge_atlas.contracts import ActionResult
    called = []
    class Remote:
        def __init__(self, project, channel):
            self.project = project

        def call(self, request):
            called.append((self.project, request.action, request.payload))
            if self.project == 'offline':
                raise AtlasError('backend_unavailable', 'service unavailable', 503)
            return ActionResult(data={'project':self.project})

        def close(self):
            pass
    monkeypatch.setattr(mcp_bridge, 'ComposeClient', Remote)
    session = mcp_bridge.SessionClient(None, 'mcp', 'initial')
    def call(name, args):
        return mcp_bridge.call_tool(session, 'atlas_atlas_'+name, args)
    assert call('open', {})['data']['project'] == 'initial'
    assert call('open', {'payload':{'project':'fde-knowledge-atlas'}})['data']['project'] == 'fde-knowledge-atlas'
    assert called[-1] == ('fde-knowledge-atlas', 'atlas.open', {})
    assert call('open', {'payload':{'project':'offline'}})['error']['code'] == 'backend_unavailable'
    assert call('open', {})['data']['project'] == 'fde-knowledge-atlas'
    assert call('status', {'atlas_id':'test'})['data']['project'] == 'fde-knowledge-atlas'
    assert call('open', {'payload':{'project':'bad name'}})['error']['code'] == 'invalid_request'
    assert mcp_bridge.SessionClient(None, 'mcp').project is None


def test_mcp_saved_instance_matches_claude_plugin_setting(tmp_path, monkeypatch):
    from knowledge_atlas.mcp_bridge import default_project
    monkeypatch.delenv('KNOWLEDGE_ATLAS_DEFAULT_PROJECT', raising=False)
    monkeypatch.delenv('KNOWLEDGE_ATLAS_DOCKER_PROJECT', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert default_project() is None
    settings = tmp_path/'knowledge-atlas/mcp.json'
    settings.parent.mkdir()
    settings.write_text('{"project":"saved"}')
    assert default_project() == 'saved'
    monkeypatch.setenv('KNOWLEDGE_ATLAS_DEFAULT_PROJECT', 'configured-in-claude')
    assert default_project() == 'configured-in-claude'


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
            assert (missing['code'], missing['status']) == ('invalid_request', 422)
            assert 'payload.project' in missing['message'] and 'payload.path' in missing['message']
            explicit = call_tool(session, 'atlas_atlas_open', {'payload': {'path': str(tmp_path/'atlas')}})
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
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                listed=await session.list_tools()
                assert any(t.name=='atlas_atlas_create' for t in listed.tools)
                response=await session.call_tool('atlas_atlas_status',{'atlas_id':'missing','payload':{}})
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
