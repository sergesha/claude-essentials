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


@pytest.fixture
def viewer():
    from knowledge_atlas.viewer.server import ViewerServer
    class Backend:
        def call(self,action,request):
            assert request['atlas_id']=='atlas-fixture'
            if action=='wiki.read':
                return {'data':{'page':{'content':'# Published page\n<script>bad</script>'}},'publication':'a'*40}
            return {'data':{'ready':'a'*40},'publication':'a'*40}
    server=ViewerServer(Backend(),'atlas-fixture',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server
    server.shutdown();server.server_close();thread.join()


def send(server,path='/api/wiki.read',body=None,headers=None):
    connection=http.client.HTTPConnection(*server.server_address,timeout=3)
    supplied={'Authorization':'Bearer '+server.token,'Content-Type':'application/json',**(headers or {})}
    connection.request('POST',path,body=json.dumps(body or {'payload':{'page_id':'Record/page'},'publication':'a'*40}),headers=supplied)
    response=connection.getresponse();data=response.read();connection.close()
    return response.status,dict(response.headers),data


@pytest.mark.parametrize('headers,status',[({'Authorization':'Bearer wrong'},401),({'Origin':'http://evil.invalid'},403),({'Host':'evil.invalid'},403)])
def test_viewer_rejects_foreign_or_unauthorized_requests(viewer,headers,status):
    assert send(viewer,headers=headers)[0]==status


def test_viewer_freezes_atlas_and_rejects_arbitrary_tools(viewer):
    assert send(viewer,path='/api/atlas.create')[0]==404
    assert send(viewer,body={'atlas_id':'foreign','payload':{}})[0]==400
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
    backend.docker_project = None

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
    backend.docker_project = 'existing'

    def run(argv, **_):
        assert argv[:4] == ['docker', 'compose', '-p', 'existing']
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
    result=subprocess.run(['sh',str(Path(__file__).parents[1]/'scripts/launch.sh'),'view','--atlas','/data/atlas'],
        env={**os.environ,'KNOWLEDGE_ATLAS_DOCKER_PROJECT':'viewer-test-missing','KNOWLEDGE_ATLAS_PYTHON':str(fake)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert capture.read_text()=='-m knowledge_atlas.cli view --atlas /data/atlas'


def test_viewer_cannot_read_unrelated_operation(viewer):
    status,_,_=send(viewer,path='/api/operation.read',body={'payload':{'operation_id':'another-atlas-operation'}})
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
    body={'publication':'a'*40,'payload':{'query':'agents','mode':'fts','count':12}}
    assert send(viewer,path='/api/search',body=body)[0]==200
    assert send(viewer,path='/api/search',body=body,headers={'Authorization':'Bearer wrong'})[0]==401
