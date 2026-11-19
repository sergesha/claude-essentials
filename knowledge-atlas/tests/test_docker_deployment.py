"""Deployment boundary checks; native components are qualified separately."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
PACKAGE = Path(__file__).resolve().parents[1]

def deployment():
    path = PACKAGE / 'docker/entrypoint.py'
    assert path.is_file(), 'Docker entrypoint is not implemented'
    spec = importlib.util.spec_from_file_location('atlas_docker_entrypoint', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_docker_launcher_preserves_arguments_and_does_not_run_native(tmp_path):
    fake = tmp_path / 'docker'
    capture = tmp_path / 'argv.json'
    fake.write_text('#!' + sys.executable + '\nimport json,os,sys\njson.dump(sys.argv[1:],open(os.environ["CAPTURE"],"w"))\n')
    fake.chmod(0o755)
    server = json.loads((PACKAGE/'.codex-plugin/mcp.json').read_text())['mcpServers']['knowledge-atlas']
    supplied = {'KNOWLEDGE_ATLAS_DOCKER_PROJECT': 'atlas-test', 'KNOWLEDGE_ATLAS_PYTHON': '/not-a-python'}
    forwarded = {key: supplied[key] for key in server['env_vars'] if key in supplied}
    base_env = {key:value for key,value in os.environ.items() if key not in supplied}
    result = subprocess.run(['sh', str(PACKAGE/'scripts/launch.sh'), 'status', '--config', '/data/a b.json'],
        env={**base_env, 'PATH': str(tmp_path)+os.pathsep+os.environ['PATH'], 'CAPTURE': str(capture), **forwarded},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = json.loads(capture.read_text())
    assert args[:5] == ['compose', '-p', 'atlas-test', '-f', str(PACKAGE/'docker/compose.yaml')]
    assert args[5:] == ['exec', '-T', 'atlas', '/opt/knowledge-atlas/.venv/bin/python',
                       '-m', 'knowledge_atlas.cli', 'status', '--config', '/data/a b.json']

def test_start_refuses_unprepared_runtime_without_creating_atlas(tmp_path, monkeypatch):
    module = deployment()
    monkeypatch.setenv('ATLAS_RUNTIME_ROOT', str(tmp_path/'runtime'))
    monkeypatch.setenv('ATLAS_DATA_ROOT', str(tmp_path/'data'))
    with pytest.raises(Exception, match='manifest|runtime'):
        module.start()
    assert not (tmp_path/'data/atlas').exists()

def test_failed_component_readiness_stops_other_children(tmp_path):
    module = deployment()
    supervisor = module.Supervisor(tmp_path)
    child = supervisor.launch('slow', [sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        failed = supervisor.launch('failed', [sys.executable, '-c', 'raise SystemExit(7)'])
        failed.wait(timeout=5)
        with pytest.raises(RuntimeError, match='failed'):
            supervisor.ready(failed, 'http://127.0.0.1:1/health', timeout=1)
    finally:
        supervisor.close()
    assert child.poll() is not None

def test_graph_init_selection_preserves_existing_database(tmp_path):
    module = deployment()
    assert module.needs_graph_init(tmp_path)
    (tmp_path/'db').mkdir()
    (tmp_path/'db/sentinel').write_text('preserve')
    assert not module.needs_graph_init(tmp_path)
    assert (tmp_path/'db/sentinel').read_text() == 'preserve'


def test_upgrade_rebinds_existing_venv_without_auth_volume(tmp_path):
    module = deployment()
    runtime = tmp_path / 'runtime'
    venv = runtime / 'python'
    (venv / 'bin').mkdir(parents=True)
    legacy = Path('/auth/.local/share/uv/python')
    build = Path('cpython-3.12.14-linux-aarch64-gnu/bin')
    stable = tmp_path / 'installed-python'
    (stable / build).mkdir(parents=True)
    (stable / build / 'python3.12').write_text('same interpreter build')
    python = venv / 'bin/python'
    python.symlink_to(legacy / build / 'python3.12')
    (venv / 'bin/python3').symlink_to('python')
    cfg = venv / 'pyvenv.cfg'
    cfg.write_text(f'home = {legacy / build}\nversion_info = 3.12.14\n')
    marker = runtime / 'runtime.json'
    marker.write_text('retained')

    module.migrate_runtime_interpreter(runtime, stable)
    module.migrate_runtime_interpreter(runtime, stable)
    assert python.resolve() == stable / build / 'python3.12'
    assert (venv / 'bin/python3').readlink() == Path('python')
    assert f'home = {stable / build}\n' in cfg.read_text()
    assert marker.read_text() == 'retained'


def test_missing_native_launcher_requires_explicit_preparation(tmp_path):
    root = tmp_path / 'installed'
    (root / 'scripts').mkdir(parents=True)
    launcher = root / 'scripts/launch.sh'
    launcher.write_bytes((PACKAGE / 'scripts/launch.sh').read_bytes())
    bins = tmp_path / 'bin'
    bins.mkdir()
    uv = bins / 'uv'
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
    uv.chmod(0o755)
    capture = tmp_path / 'calls'
    env = {
        'HOME': str(tmp_path), 'PATH': str(bins) + os.pathsep + os.defpath,
        'CAPTURE': str(capture),
    }
    result = subprocess.run(['sh', launcher], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'prepare-launcher' in result.stderr
    assert not capture.exists()
    subprocess.run(['sh', launcher, 'prepare-launcher'], env=env, check=True)
    assert capture.read_text().splitlines() == [
        'sync', '--project', str(root), '--locked', '--no-dev', '--python', '3.12']
    venv = root / '.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'python').symlink_to(sys.executable)
    (root / 'src/knowledge_atlas').mkdir(parents=True)
    (root / 'src/knowledge_atlas/cli.py').write_text('import sys; print(sys.argv[1:])')
    result = subprocess.run(['sh', launcher, 'status'], env=env, capture_output=True,
                            check=True, text=True)
    assert result.stdout.strip() == "['status']"


def test_graph_credential_initialization_and_legacy_rotation(tmp_path):
    import hashlib
    runtime = tmp_path / 'runtime'
    repo = runtime / 'terminusdb'
    swi = runtime / 'native-tools/swipl/bin/swipl'
    (repo / 'src/rust').mkdir(parents=True)
    (repo / 'src/rust/Cargo.lock').write_text('[[package]]\nname = "vectorlink"\nversion = "2.0.0"\n')
    swi.parent.mkdir(parents=True)
    swi.write_text('#!' + sys.executable + '\n' + """
import hashlib, os, sys
from pathlib import Path
secret = sys.stdin.readline().strip()
assert len(secret) >= 32 and all(secret not in arg for arg in sys.argv)
db = Path(os.environ['TERMINUSDB_SERVER_DB_PATH'])
mode = 'rotate' if db.is_dir() else 'init'
assert ('user,password' if mode == 'rotate' else 'store,init') in sys.argv[sys.argv.index('-g')+1]
db.mkdir(exist_ok=True)
(db / 'digest').write_text(hashlib.sha256(secret.encode()).hexdigest())
""")
    swi.chmod(0o755)
    server = repo / 'terminusdb'
    server.write_text('#!/bin/sh\nexit 0\n')
    server.chmod(0o755)
    tokenizer = tmp_path / 'tokenizer'
    tokenizer.write_text('fixture')
    config = {'endpoint': {'tokenizer_sha256': hashlib.sha256(tokenizer.read_bytes()).hexdigest()},
              'plugin_profile': {'index_algorithm': 'FLAT'}}
    profile = tmp_path / 'profile'
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    profile.write_text(json.dumps({'profile': config, 'profile_id': digest, 'model_namespace': 'fixture'}))
    base = [sys.executable, PACKAGE / 'runtime/native-runtime.py', '--runtime', runtime,
            '--profile', profile, '--tokenizer', tokenizer, '--provider', 'http://127.0.0.1:9402',
            '--port', '9403']
    observed = set()
    for name in ('new', 'legacy'):
        state = tmp_path / name
        if name == 'legacy':
            (state / 'db').mkdir(parents=True)
            (state / 'db/publications').write_text('retained')
        subprocess.run([*base, '--state', state, 'serve' if name == 'legacy' else 'init'], check=True)
        key = state / 'admin.key'
        assert key.stat().st_mode & 0o777 == 0o600
        secret = key.read_text().strip()
        assert secret not in observed
        observed.add(secret)
        assert (state / 'db/digest').read_text() == hashlib.sha256(secret.encode()).hexdigest()
        subprocess.run([*base, '--state', state, 'serve'], check=True)
        assert key.read_text().strip() == secret
    assert (tmp_path / 'legacy/db/publications').read_text() == 'retained'


def test_graph_client_uses_deployment_key_only_for_the_local_request(tmp_path, monkeypatch):
    from base64 import b64encode
    from io import BytesIO
    from knowledge_atlas.core import graph_stage

    key = tmp_path / 'admin.key'
    key.write_text('test-fixture-key\n')
    monkeypatch.setenv('KNOWLEDGE_ATLAS_GRAPH_KEY_FILE', str(key))
    requests = []

    def response(request, timeout):
        requests.append(request)
        body = BytesIO(b'{}')
        body.headers = {}
        body.status = 200
        return body

    monkeypatch.setattr(graph_stage.urllib.request, 'urlopen', response)
    graph = graph_stage.GraphStage('http://127.0.0.1:9403', 'admin/atlas')
    graph.call('GET', '/api/users')
    assert requests[0].unredirected_hdrs['Authorization'] == 'Basic ' + b64encode(b'admin:test-fixture-key').decode()
    assert 'Authorization' not in requests[0].headers
    assert 'test-fixture-key' not in str(graph.trace)
    with pytest.raises(ValueError, match='local graph endpoint'):
        graph_stage.GraphStage('http://example.invalid', 'admin/atlas').call('GET', '/api/users')
    assert len(requests) == 1
