"""Native qualification is opt-in and never inferred from controlled fixtures."""
from contextlib import contextmanager
from functools import partial
import pytest


def pytest_addoption(parser):
    parser.addoption('--runtime-config', help='Prepared runtime/archive qualification JSON')
    parser.addoption('--provider', choices=('codex','claude'))
    parser.addoption('--host', choices=('codex','claude'))
    parser.addoption('--evidence-dir', help='Unused external lifecycle evidence directory')
    parser.addoption('--profile-config', help='Explicit six-stage provider JSON')


@pytest.fixture
def runtime_configuration(request):
    import json
    from pathlib import Path
    path = request.config.getoption('--runtime-config')
    if not path:
        pytest.skip('native archive qualification requires --runtime-config')
    return json.loads(Path(path).read_text())


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    from pathlib import Path
    selected = request.config.getoption('--evidence-dir')
    if selected:
        path = Path(selected).resolve()
        path.mkdir(parents=True, exist_ok=False)
        return path
    return tmp_path_factory.mktemp(request.node.name)


@pytest.fixture
def native_graph_session(monkeypatch):
    return partial(_native_graph, monkeypatch=monkeypatch)


@pytest.fixture
def native_graph(runtime_configuration, tmp_path, native_graph_session):
    with native_graph_session(runtime_configuration, tmp_path) as graph:
        yield graph


@contextmanager
def _native_graph(runtime_configuration, tmp_path, monkeypatch):
    """Launch only this test's pinned native processes; retain pytest artifacts."""
    import json
    import os
    from pathlib import Path
    import socket
    import subprocess
    import time
    import urllib.request
    from knowledge_atlas.core.graph_stage import GraphStage
    runtime = Path(runtime_configuration['runtime_root'])
    package = Path(__file__).resolve().parents[2]
    if runtime_configuration.get('archive'):
        from knowledge_atlas.core.archive import restore_atlas
        from knowledge_atlas.core.embedding_provider import merge_verified_cache
        restore_atlas(runtime_configuration['archive'], tmp_path/'atlas')
        merge_verified_cache(tmp_path/'atlas/vectors', tmp_path/'service/vectors')
    ports = []
    for _ in range(3):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); ports.append(sock.getsockname()[1])
    raw_port, bridge_port, graph_port = ports
    monkeypatch.setenv('KNOWLEDGE_ATLAS_GRAPH_KEY_FILE', str(tmp_path/'graph/admin.key'))
    processes = []; logs = []
    env = {**os.environ, 'PYTHONPATH':str(package/'src'),
           'EMBED_MODEL_PATH':str(runtime/'model'), 'EMBED_PORT':str(raw_port)}
    def launch(name, argv):
        log = (tmp_path/(name+'.log')).open('w'); logs.append(log)
        process = subprocess.Popen(argv, env=env, stdout=log, stderr=log)
        processes.append(process)
        return process
    def wait(process, endpoint):
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            assert process.poll() is None, 'native process exited; inspect retained logs'
            try:
                with urllib.request.urlopen(endpoint, timeout=1): return
            except OSError: time.sleep(.2)
        pytest.fail('native endpoint did not become ready: '+endpoint)
    python = str(runtime/'python/bin/python')
    base = [python, str(package/'runtime/native-runtime.py'), '--runtime',str(runtime),
            '--state',str(tmp_path/'graph'), '--profile',str(package/'runtime/embedding-profile.json'),
            '--tokenizer',str(runtime/'model/tokenizer.json'), '--provider',f'http://127.0.0.1:{bridge_port}',
            '--port',str(graph_port)]
    try:
        raw = launch('gemma', [python,str(package/'runtime/gemma-service.py')])
        wait(raw, f'http://127.0.0.1:{raw_port}/health')
        bridge = launch('bridge',[python,str(package/'runtime/embedding-provider.py'),
            '--profile',str(package/'runtime/embedding-profile.json'), '--cache',str(tmp_path/'service/vectors'),
            '--endpoint',f'http://127.0.0.1:{raw_port}', '--port',str(bridge_port)])
        wait(bridge, f'http://127.0.0.1:{bridge_port}/stats')
        initialized = launch('graph-init',base+['init'])
        assert initialized.wait(timeout=90)==0, (tmp_path/'graph-init.log').read_text()
        server = launch('graph-serve',base+['serve'])
        graph = GraphStage(f'http://127.0.0.1:{graph_port}', 'admin/qualification')
        wait(server, graph.url+'/api/v1/health')
        def switch_cache(cache):
            nonlocal bridge
            bridge.terminate();bridge.wait(timeout=10)
            bridge = launch('bridge-'+str(len(processes)),[python,str(package/'runtime/embedding-provider.py'),
                '--profile',str(package/'runtime/embedding-profile.json'),'--cache',str(cache),
                '--endpoint',f'http://127.0.0.1:{raw_port}','--port',str(bridge_port)])
            wait(bridge, f'http://127.0.0.1:{bridge_port}/stats')
        def crash_restart_graph():
            nonlocal server
            server.kill()
            server.wait(timeout=10)
            assert server.returncode == -9
            server = launch('graph-restart-'+str(len(processes)),base+['serve'])
            wait(server, graph.url+'/api/v1/health')
        yield {'runtime':runtime, 'graph':graph, 'atlas':tmp_path/'atlas',
               'cache':tmp_path/'service/vectors', 'crash_restart_graph':crash_restart_graph, 'switch_cache':switch_cache,
               'bridge':f'http://127.0.0.1:{bridge_port}', 'raw':f'http://127.0.0.1:{raw_port}'}
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill();process.wait()
        for log in logs: log.close()
