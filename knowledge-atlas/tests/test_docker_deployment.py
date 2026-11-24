"""Deployment boundary checks; native components are qualified separately."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
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

def test_launcher_ignores_removed_compose_default(tmp_path):
    docker = tmp_path / 'docker'
    capture = tmp_path / 'docker-called'
    docker.write_text(f'#!/bin/sh\ntouch "{capture}"\nexit 1\n')
    docker.chmod(0o755)
    fake_python = tmp_path / 'python'
    fake_python.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    fake_python.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'KNOWLEDGE_ATLAS_DOCKER_PROJECT': 'atlas-test', 'KNOWLEDGE_ATLAS_PYTHON': str(fake_python)}
    result = subprocess.run(['sh', str(PACKAGE / 'scripts/launch.sh'),
                             'status', '--config', '/data/a b.json'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ['-m', 'knowledge_atlas.cli', 'status', '--config', '/data/a b.json']
    assert not capture.exists()


def test_plugin_mcp_launchers_request_no_compose_default():
    for name in ('.mcp.json', '.claude-plugin/mcp.json', '.claude-plugin/plugin.json', '.codex-plugin/mcp.json'):
        manifest = json.loads((PACKAGE / name).read_text())
        server = manifest['mcpServers']['knowledge-atlas']
        assert server['args'][-1] == 'mcp'
        assert 'project' not in manifest.get('userConfig', {})
        for stale in ('KNOWLEDGE_ATLAS_DEFAULT_PROJECT', 'KNOWLEDGE_ATLAS_DOCKER_PROJECT'):
            assert stale not in server.get('env', {})


def test_installed_codex_launcher_reports_legacy_docker_selector(tmp_path):
    installed = tmp_path / 'installed'
    for path in ('.codex-plugin/mcp.json', 'scripts/launch.sh'):
        target = installed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / path, target)
    shutil.copytree(PACKAGE / 'src', installed / 'src', ignore=shutil.ignore_patterns('__pycache__'))

    manifest = json.loads((installed / '.codex-plugin/mcp.json').read_text())
    server = manifest['mcpServers']['knowledge-atlas']
    supplied = {
        'KNOWLEDGE_ATLAS_DOCKER_PROJECT': 'previous-compose-deployment',
        'KNOWLEDGE_ATLAS_PYTHON': sys.executable,
        'XDG_DATA_HOME': str(tmp_path / 'data'),
        'XDG_CONFIG_HOME': str(tmp_path / 'config'),
    }
    # Codex clears the child environment and forwards its defaults plus env_vars.
    env = {'PATH': os.defpath, 'HOME': str(tmp_path),
           **{name: supplied[name] for name in server['env_vars'] if name in supplied}}
    result = subprocess.run([server['command'], *server['args']], cwd=installed / server['cwd'],
                            env=env, input='', capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert 'KNOWLEDGE_ATLAS_DOCKER_PROJECT' in result.stderr
    assert 'atlas.connect' in result.stderr and 'payload.container_name' in result.stderr
    assert 'previous-compose-deployment' not in result.stderr
    assert not (tmp_path / 'data').exists()


def test_legacy_compose_stop_dispatches_to_neither_service(tmp_path):
    from knowledge_atlas.contracts import ServiceContext
    from knowledge_atlas.http_api import create_app

    native_data = tmp_path / 'native'
    native_root = native_data / 'knowledge-atlas' / 'service'
    container_root = tmp_path / 'container-service'
    container_config = tmp_path / 'container.json'
    container_config.write_text(json.dumps({'service_root': str(container_root)}))
    docker = tmp_path / 'docker'
    docker.write_text(f'#!{sys.executable}\n'
                      'import os, sys\n'
                      'os.execv(sys.executable, [sys.executable, "-m", "knowledge_atlas.cli", "stop", '
                      f'"--config", {str(container_config)!r}])\n')
    docker.chmod(0o755)
    env = {'HOME': str(tmp_path), 'PATH': str(tmp_path) + os.pathsep + os.defpath,
           'XDG_DATA_HOME': str(native_data), 'KNOWLEDGE_ATLAS_PYTHON': sys.executable,
           'KNOWLEDGE_ATLAS_DOCKER_PROJECT': 'previous-compose-deployment'}

    with ExitStack() as cleanup:
        native = create_app(ServiceContext(native_root)).start()
        cleanup.callback(native.stop, wait=True)
        container = create_app(ServiceContext(container_root)).start()
        cleanup.callback(container.stop, wait=True)
        result = subprocess.run(['sh', str(PACKAGE / 'scripts/launch.sh'), 'stop'], env=env,
                                capture_output=True, text=True, timeout=10)
        assert not native.stopping, 'legacy stop reached the default native service'
        assert not container.stopping, 'legacy stop reached the Docker service'
        assert result.returncode != 0
        assert result.stdout == ''
        assert 'KNOWLEDGE_ATLAS_DOCKER_PROJECT' in result.stderr
        assert 'docker stop' in result.stderr and 'stop --config' in result.stderr


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


def test_mcp_launcher_retries_failed_setup_and_reuses_ready_environment(tmp_path):
    root = tmp_path / 'installed'
    (root / 'scripts').mkdir(parents=True)
    launcher = root / 'scripts/launch.sh'
    launcher.write_bytes((PACKAGE / 'scripts/launch.sh').read_bytes())
    bins = tmp_path / 'bin'
    bins.mkdir()
    uv = bins / 'uv'
    uv.write_text("""#!/bin/sh
echo setup >> "$CAPTURE"
printf '%s\\n' "$@" > "$CAPTURE.args"
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
ln -sf "$TEST_PYTHON" "$UV_PROJECT_ENVIRONMENT/bin/python"
echo "Installing dependencies"
exit "${UV_TEST_EXIT:-0}"
""")
    uv.chmod(0o755)
    capture = tmp_path / 'calls'
    env = {
        'HOME': str(tmp_path), 'PATH': str(bins) + os.pathsep + os.defpath,
        'CAPTURE': str(capture), 'TEST_PYTHON': sys.executable,
    }
    (root / 'src/knowledge_atlas').mkdir(parents=True)
    (root / 'src/knowledge_atlas/cli.py').write_text('import sys; print(sys.argv[1:])')
    failed = subprocess.run(['sh', launcher], env={**env, 'UV_TEST_EXIT': '7'},
                            capture_output=True, text=True)
    assert failed.returncode == 7
    assert failed.stdout == ''
    for arguments in ([], ['mcp']):
        result = subprocess.run(['sh', launcher, *arguments], env=env,
                                check=True, capture_output=True, text=True)
        assert result.stdout.strip() == "['mcp']"
    assert capture.read_text().splitlines() == ['setup', 'setup']
    assert (tmp_path / 'calls.args').read_text().splitlines() == [
        'sync', '--project', str(root), '--locked', '--no-dev', '--python', '3.12']
    subprocess.run(['sh', launcher, 'prepare-launcher'], env=env, check=True)
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


def startup_config(tmp_path, monkeypatch):
    module = deployment()
    data = tmp_path / 'data'
    data.mkdir(exist_ok=True)
    profiles = json.loads((PACKAGE / 'runtime/model-profiles.json').read_text())
    (data / 'profiles.json').write_text(json.dumps({k: {**v, 'provider': 'codex'} for k, v in profiles.items()}))
    monkeypatch.setenv('ATLAS_DATA_ROOT', str(data))
    monkeypatch.setenv('ATLAS_RUNTIME_ROOT', str(tmp_path / 'runtime'))
    monkeypatch.setattr(module, 'validate_runtime', lambda *_: None)
    monkeypatch.setattr(module.signal, 'signal', lambda *_: None)
    calls = {}
    def launch(self, name, argv, env):
        calls[name] = argv
        if name == 'bridge':
            raise KeyboardInterrupt
    monkeypatch.setattr(module.Supervisor, 'launch', launch)
    monkeypatch.setattr(module.Supervisor, 'ready', lambda *_: None)
    module.start()
    return json.loads((data / 'service-config.json').read_text()), calls['bridge']


def test_start_has_no_single_project_binding(tmp_path, monkeypatch):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects

    config, _ = startup_config(tmp_path, monkeypatch)
    assert config['configuration']['graph'] == {'url': 'http://127.0.0.1:9403'}
    assert 'atlas_path' not in config['configuration']
    assert list_projects(service_context(config)) == []
    assert not (tmp_path / 'data/atlas').exists()


def test_runtime_cache_is_outside_projects(tmp_path, monkeypatch):
    config, argv = startup_config(tmp_path, monkeypatch)
    assert Path(argv[argv.index('--cache') + 1]) == Path(config['service_root']) / 'vectors'


def test_project_registry_survives_container_restart(tmp_path, monkeypatch):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project

    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    # This formerly bound path may also hold an ordinary new project.
    ids = {create_project(service, tmp_path / 'data' / name) for name in ('atlas', 'second')}
    prior = list_projects(service)
    assert {p.atlas_id for p in prior} == ids
    again, _ = startup_config(tmp_path, monkeypatch)
    assert list_projects(service_context(again)) == prior


def test_container_project_paths_stay_on_data_volume(tmp_path, monkeypatch):
    from knowledge_atlas.contracts import AtlasError
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.operations import validate_atlas_path

    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    data = tmp_path / 'data'
    (data / 'escape').symlink_to(tmp_path)
    assert validate_atlas_path(service, data / 'projects/a') == data / 'projects/a'
    for path in (data, tmp_path / 'auth/project', tmp_path / 'runtime/project', data / 'graph/project',
                 data / 'service/project', data / 'logs/project', data / 'escape/outside'):
        with pytest.raises(AtlasError, match='invalid_location'):
            validate_atlas_path(service, path)


def test_stopped_legacy_snapshot_remains_recoverable(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    from knowledge_atlas.contracts import AtlasError
    from knowledge_atlas.core.git_store import git
    from knowledge_atlas.core.publication import Publications
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import legacy_publication, vector_fixture

    data = tmp_path / 'data'
    data.mkdir()
    identity, store, ready = legacy_publication(data / 'atlas')
    original, _ = vector_fixture(store.base / 'vectors')
    refs = git(store.root, 'show-ref')
    (data / 'graph/db').mkdir(parents=True)
    (data / 'graph/db/fixture').write_bytes(b'stopped native state marker')
    (data / 'service').mkdir()
    with sqlite3.connect(data / 'service/operations.sqlite3') as db:
        db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        db.execute('INSERT INTO atlases VALUES (?,?)', (identity, str(store.base)))
    prior_config = '{"configuration":{"atlas_path":"legacy"}}'
    (data / 'service-config.json').write_text(prior_config)
    backup = tmp_path / 'stopped-backup'
    shutil.copytree(data, backup)

    with original.connect() as db:
        db.execute("UPDATE vector_integrity SET sha256='damaged'")
    with pytest.raises(AtlasError, match='integrity'):
        startup_config(tmp_path, monkeypatch)
    assert (data / 'service-config.json').read_text() == prior_config
    assert git(store.root, 'show-ref') == git(backup / 'atlas/atlas', 'show-ref') == refs
    assert Publications(store).ready() == ready
    with sqlite3.connect(backup / 'service/operations.sqlite3') as db:
        assert [row[1] for row in db.execute('PRAGMA table_info(atlases)')] == ['atlas_id', 'path']
    shutil.copy2(backup / 'atlas/vectors/vectors.sqlite3', original.cache / 'vectors.sqlite3')
    config, _ = startup_config(tmp_path, monkeypatch)
    project, = list_projects(service_context(config))
    assert (project.atlas_id, project.path, project.graph_database) == (identity, store.base, 'admin/atlas')
    assert (data / 'graph/db/fixture').read_bytes() == (backup / 'graph/db/fixture').read_bytes()


def test_restart_with_registered_child_of_legacy_path(tmp_path, monkeypatch):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project

    config, _ = startup_config(tmp_path, monkeypatch)
    path = tmp_path / 'data/atlas/notes'
    identity = create_project(service_context(config), path)
    restarted, _ = startup_config(tmp_path, monkeypatch)
    binding, = list_projects(service_context(restarted))
    assert (binding.atlas_id, binding.path) == (identity, path)


@pytest.mark.parametrize('mount', ['missing', 'empty', 'cache-only'])
def test_start_refuses_hidden_registered_legacy_mount(tmp_path, monkeypatch, mount):
    import sqlite3
    from knowledge_atlas.contracts import AtlasError
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.publication import Publications
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import legacy_publication, vector_fixture

    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'atlas'
    identity, _, ready = legacy_publication(path)
    original = tmp_path / 'original-mount'
    path.rename(original)
    if mount == 'empty':
        path.mkdir()
    elif mount == 'cache-only':
        vector_fixture(path / 'vectors')
    (data / 'service').mkdir()
    with sqlite3.connect(data / 'service/operations.sqlite3') as db:
        db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        db.execute('INSERT INTO atlases VALUES (?,?)', (identity, str(path)))
    config_path = data / 'service-config.json'
    config_path.write_bytes(b'previous deployment configuration')
    with pytest.raises(AtlasError, match='invalid_atlas|atlas_conflict'):
        startup_config(tmp_path, monkeypatch)
    assert config_path.read_bytes() == b'previous deployment configuration'
    assert not (data / 'service/vectors').exists()
    assert not (data / 'logs').exists()

    if path.exists():
        path.rename(tmp_path / 'hidden-mount')
    original.rename(path)
    config, _ = startup_config(tmp_path, monkeypatch)
    binding, = list_projects(service_context(config))
    assert (binding.atlas_id, binding.graph_database) == (identity, 'admin/atlas')
    assert Publications(GitStore(path)).ready() == ready


@pytest.mark.parametrize('origin', ['ordinary', 'adopted'])
@pytest.mark.parametrize('mount', ['missing', 'different-repository'])
def test_restart_serves_other_projects_after_completed_binding(tmp_path, monkeypatch, origin, mount):
    from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project, legacy_publication

    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'atlas'
    if origin == 'adopted':
        identity, _, _ = legacy_publication(path)
    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    if origin == 'ordinary':
        identity = create_project(service, path)
    other = create_project(service, data / 'second')
    before = list_projects(service)
    binding = next(project for project in before if project.atlas_id == identity)
    assert (binding.graph_database == 'admin/atlas') == (origin == 'adopted')
    path.rename(tmp_path / 'original-mount')
    if mount == 'different-repository':
        create_project(ServiceContext(tmp_path / 'other-service'), path)

    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    assert list_projects(service) == before
    projects = invoke(service, ActionRequest('atlas.list'))[1].data['projects']
    assert {p['atlas_id']: p['status'] for p in projects} == {identity: 'unavailable', other: 'unpublished'}
    assert invoke(service, ActionRequest('atlas.open', atlas_id=other))[1].data['atlas_id'] == other
    with pytest.raises(AtlasError, match='atlas_conflict'):
        invoke(service, ActionRequest('atlas.open', atlas_id=identity))
