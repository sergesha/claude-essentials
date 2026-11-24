"""Real HTTP boundary tests; external MCP calls replaced by a bounded fixture."""
import http.client
import json
import threading
import pytest


def test_untrusted_markdown_cannot_execute_or_load_remote_content():
    from knowledge_atlas.viewer.server import render_markdown
    value=render_markdown('<script>alert(1)</script>\n![track](https://evil.invalid/x)\n[x](javascript:alert(1))\n[ok](https://example.org)')
    assert '<script' not in value and '<img' not in value and 'href="javascript:' not in value
    assert '&lt;script&gt;' in value and 'href="https://example.org"' in value


ATLAS_A = '10000000-0000-4000-8000-000000000001'
ATLAS_B = '10000000-0000-4000-8000-000000000002'
UNKNOWN_ATLAS = '10000000-0000-4000-8000-000000000003'


@pytest.fixture
def viewer():
    from knowledge_atlas.viewer.backend import MCPBackend
    from knowledge_atlas.viewer.server import ViewerServer

    backend = object.__new__(MCPBackend)
    backend.container_id = None
    backend.requests = []

    def call(name, request):
        action = name.removeprefix('atlas_').replace('_', '.')
        backend.requests.append((action, request))
        if action == 'atlas.list':
            assert not request
            return {'data': {'projects': [
                {'atlas_id': ATLAS_A, 'name': 'Shared name', 'publication': 'a'*40, 'status': 'ready'},
                {'atlas_id': ATLAS_B, 'name': 'Shared name', 'publication': None, 'status': 'unpublished'},
            ]}}
        atlas_id = request['atlas_id']
        if atlas_id not in {ATLAS_A, ATLAS_B}:
            return {'error': {'code': 'atlas_not_found', 'message': 'Atlas is not registered', 'status': 404}}
        if action == 'atlas.open':
            return {'data': {'atlas_id': atlas_id}}
        if action == 'wiki.read':
            return {'data': {'page': {'id': atlas_id, 'content': '# Published page\n<script>bad</script>'}}, 'publication': 'a'*40}
        if action == 'candidate.decide':
            return {'operation_id': 'admitted', 'state': 'running'}
        if action == 'operation.read':
            return {'operation_id': request['payload']['operation_id'], 'state': 'running'}
        if action == 'atlas.status' and atlas_id == ATLAS_A:
            return {'data': {'active_operations': [{'operation_id': 'active'}]}, 'publication': 'a'*40}
        return {'data': {}, 'publication': None}

    backend._call = call
    server = ViewerServer(backend, ATLAS_A, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def send(server,path='/api/wiki.read',body=None,headers=None):
    connection=http.client.HTTPConnection(*server.server_address,timeout=3)
    supplied={'Authorization':'Bearer '+server.token,'Content-Type':'application/json',**(headers or {})}
    connection.request('POST',path,body=json.dumps(body if body is not None else {'atlas_id':ATLAS_A,'payload':{'page_id':'Record/page'},'publication':'a'*40}),headers=supplied)
    response=connection.getresponse();data=response.read();connection.close()
    return response.status,dict(response.headers),data


@pytest.mark.parametrize('headers,status',[({'Authorization':'Bearer wrong'},401),({'Origin':'http://evil.invalid'},403),({'Host':'evil.invalid'},403)])
def test_viewer_rejects_foreign_or_unauthorized_requests(viewer,headers,status):
    assert send(viewer,headers=headers)[0]==status


def test_viewer_requires_atlas_uuid_and_rejects_arbitrary_tools(viewer):
    assert send(viewer,path='/api/atlas.create')[0]==404
    assert send(viewer,body={'atlas_id':'/host/path','payload':{'page_id':'Record/page'}})[0]==400
    assert send(viewer,body={'payload':{'page_id':'Record/page'}})[0]==400
    assert send(viewer,body={'atlas_id':ATLAS_A,'payload':{'page_id':'Record/page'},'unexpected':True})[0]==400
    status,headers,body=send(viewer)
    assert status==200
    result=json.loads(body)
    assert result['publication']=='a'*40
    assert '<h1>Published page</h1>' in result['data']['page']['html']
    assert '<script' not in result['data']['page']['html']
    assert headers['Cache-Control']=='no-store'
    assert "default-src 'none'" in headers['Content-Security-Policy']


def test_viewer_rejects_oversize_body_before_read(viewer):
    assert send(viewer,headers={'Content-Length':str(2*1024*1024)})[0]==413


def test_viewer_resolves_explicit_native_path_to_uuid(tmp_path):
    from knowledge_atlas.contracts import ServiceContext
    from knowledge_atlas.viewer.backend import MCPBackend
    from test_project_registry import create_project

    path = tmp_path / 'notes'
    identity = create_project(ServiceContext(tmp_path / 'service'), path)
    backend = object.__new__(MCPBackend)
    backend.container_id = None

    def call(action, request):
        assert action == 'atlas.open'
        assert request == {'atlas_id': identity}
        return {'data': {'atlas_id': identity}}

    backend.call = call
    assert backend.open_atlas(str(path)) == identity
    assert backend.open_atlas(identity) == identity


def test_viewer_resolves_docker_path_inside_selected_container(monkeypatch):
    import subprocess
    import uuid
    from types import SimpleNamespace
    from knowledge_atlas.viewer import backend as viewer_backend

    identity = str(uuid.uuid4())
    backend = object.__new__(viewer_backend.MCPBackend)
    backend.container_id = 'a' * 64

    def run(argv, **_):
        assert argv[:4] == ['docker', 'exec', '-i', 'a' * 64]
        assert argv[-1] == '/data/atlas'
        return SimpleNamespace(stdout=identity + '\n')

    def call(action, request):
        assert action == 'atlas.open'
        assert request == {'atlas_id': identity}
        return {'data': {'atlas_id': identity}}

    monkeypatch.setattr(subprocess, 'run', run)
    backend.call = call
    assert backend.open_atlas('/data/atlas') == identity


def test_docker_view_command_runs_on_host(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    fake=tmp_path/'python'
    capture=tmp_path/'arguments'
    fake.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nPath('+repr(str(capture))+').write_text(" ".join(sys.argv[1:]))\n')
    fake.chmod(0o755)
    result=subprocess.run(['sh',str(Path(__file__).parents[1]/'scripts/launch.sh'),'view','--atlas','/data/atlas','--container-name','atlas-test'],
        env={**os.environ,'KNOWLEDGE_ATLAS_PYTHON':str(fake)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert capture.read_text()=='-m knowledge_atlas.cli view --atlas /data/atlas --container-name atlas-test'


def test_viewer_cannot_read_unrelated_operation(viewer):
    status,_,_=send(viewer,path='/api/operation.read',body={'atlas_id':ATLAS_A,'payload':{'operation_id':'another-atlas-operation'}})
    assert status==404


def test_refresh_is_outside_collapsible_sidebar_footer():
    """The compact layout hides footer copy; refresh must remain independently available."""
    from html.parser import HTMLParser
    from knowledge_atlas.viewer.server import ASSETS
    class Controls(HTMLParser):
        def __init__(self):
            super().__init__(); self.stack=[]; self.refresh=None
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if attrs.get('id')=='refresh': self.refresh=list(self.stack)
            if tag not in {'meta','link','input','br','hr'}: self.stack.append((tag,attrs))
        def handle_endtag(self,tag):
            for i in range(len(self.stack)-1,-1,-1):
                if self.stack[i][0]==tag:
                    del self.stack[i:];break
    controls=Controls();controls.feed((ASSETS/'index.html').read_text())
    assert controls.refresh is not None
    assert all('sidebar-bottom' not in attrs.get('class','').split() for _,attrs in controls.refresh)


def test_reading_html_omits_front_matter_without_losing_article():
    from knowledge_atlas.viewer.server import render_markdown
    source='---\ntitle: Article\ntags: [agents]\n---\n\n## Findings\nEvidence remains.\n'
    rendered=render_markdown(source)
    assert 'tags:' not in rendered and 'title:' not in rendered
    assert '<h2>Findings</h2>' in rendered and 'Evidence remains.' in rendered
    assert 'unfinished' in render_markdown('---\nunfinished')


@pytest.mark.parametrize('path', ['/graph-model.mjs', '/vendor/cytoscape.esm.min.mjs'])
def test_graph_assets_are_local_and_retain_security_headers(viewer, path):
    connection=http.client.HTTPConnection(*viewer.server_address,timeout=3)
    connection.request('GET',path)
    response=connection.getresponse();body=response.read();connection.close()
    assert response.status==200
    assert len(body)>100
    assert response.getheader('Content-Type').startswith('text/javascript')
    assert "script-src 'self'" in response.getheader('Content-Security-Policy')


def test_graph_search_keeps_existing_authenticated_read_boundary(viewer):
    body={'atlas_id':ATLAS_A,'publication':'a'*40,'payload':{'query':'agents','mode':'fts','count':12}}
    assert send(viewer,path='/api/search',body=body)[0]==200
    assert send(viewer,path='/api/search',body=body,headers={'Authorization':'Bearer wrong'})[0]==401


def test_viewer_lists_and_opens_registered_projects(viewer):
    status, _, body = send(viewer, '/api/atlas.list', {})
    assert status == 200
    result = json.loads(body)
    assert result['launch_atlas_id'] == ATLAS_A
    assert [(row['atlas_id'], row['name'], row['publication']) for row in result['data']['projects']] == [
        (ATLAS_A, 'Shared name', 'a'*40), (ATLAS_B, 'Shared name', None),
    ]
    assert send(viewer, '/api/atlas.list', {'atlas_id': ATLAS_B})[0] == 400
    assert send(viewer, '/api/atlas.list', {}, headers={'Authorization': 'Bearer wrong'})[0] == 401

    status, _, body = send(viewer, '/api/atlas.open', {'atlas_id': ATLAS_B})
    assert (status, json.loads(body)['data']['atlas_id']) == (200, ATLAS_B)
    assert send(viewer, '/api/atlas.open', {'atlas_id': UNKNOWN_ATLAS})[0] == 404
    assert send(viewer, '/api/atlas.open', {'atlas_id': '/data/atlas'})[0] == 400
    for atlas_id in (ATLAS_B, ATLAS_A):
        status, _, body = send(viewer, body={'atlas_id': atlas_id, 'payload': {'page_id': 'Record/page'}})
        assert (status, json.loads(body)['data']['page']['id']) == (200, atlas_id)


def test_viewer_rejects_cross_project_operation_poll(viewer):
    decision = {
        'atlas_id': ATLAS_A, 'request_id': '30000000-0000-4000-8000-000000000001', 'expected_publication': 'a'*40,
        'payload': {'candidate': 'proposal-a', 'action': 'accept', 'actor': 'Reviewer',
                    'kind': 'human', 'reason': 'Reviewed evidence', 'decided_at': '2026-11-25T12:00:00Z'},
    }
    assert send(viewer, '/api/candidate.decide', decision)[0] == 200
    assert send(viewer, '/api/atlas.status', {'atlas_id': ATLAS_A})[0] == 200
    assert send(viewer, '/api/atlas.open', {'atlas_id': ATLAS_B})[0] == 200
    for operation_id in ('admitted', 'active'):
        payload = {'operation_id': operation_id}
        assert send(viewer, '/api/operation.read', {'atlas_id': ATLAS_B, 'payload': payload})[0] == 404
        status, _, body = send(viewer, '/api/operation.read', {'atlas_id': ATLAS_A, 'payload': payload})
        assert (status, json.loads(body)['operation_id']) == (200, operation_id)
    assert [(action, body['atlas_id']) for action, body in viewer.backend.requests if action == 'operation.read'] == [
        ('operation.read', ATLAS_A), ('operation.read', ATLAS_A),
    ]
