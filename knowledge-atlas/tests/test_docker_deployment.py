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
    data = tmp_path / 'data'
    data.mkdir()
    profiles = json.loads((PACKAGE / 'runtime/model-profiles.json').read_text())
    (data / 'profiles.json').write_text(json.dumps({k: {**v, 'provider': 'codex'} for k, v in profiles.items()}))
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
    with pytest.raises(ValueError, match='graph'):
        module.needs_graph_init(tmp_path)
    assert (tmp_path/'db/sentinel').read_text() == 'preserve'


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
    assert not (tmp_path / '.config/knowledge-atlas').exists()
    assert capture.read_text().splitlines() == ['setup', 'setup']
    assert (tmp_path / 'calls.args').read_text().splitlines() == [
        'sync', '--project', str(root), '--locked', '--no-dev', '--python', '3.12']
    subprocess.run(['sh', launcher, 'prepare-launcher'], env=env, check=True)
    result = subprocess.run(['sh', launcher, 'status'], env=env, capture_output=True,
                            check=True, text=True)
    assert result.stdout.strip() == "['status']"


@pytest.fixture
def graph_cli(tmp_path):
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
assert not db.exists()
assert 'store,init' in sys.argv[sys.argv.index('-g')+1]
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
    return base, swi


def test_graph_init_rejects_linked_state_before_writing(tmp_path, graph_cli):
    base, _ = graph_cli
    target = tmp_path / 'empty-target'
    target.mkdir()
    link = tmp_path / 'graph'
    link.symlink_to(target, target_is_directory=True)
    result = subprocess.run([*base, '--state', link, 'init'], capture_output=True, text=True)
    assert result.returncode != 0 and 'unsupported graph' in result.stderr
    assert link.is_symlink() and list(target.iterdir()) == []


def test_graph_credential_initialization_interruption_and_current_restart(tmp_path, graph_cli):
    import hashlib
    base, swi = graph_cli
    state = tmp_path / 'new'
    run = lambda *args: subprocess.run([*base, '--state', state, *args], capture_output=True, text=True)
    original_script = swi.read_text()
    swi.write_text(original_script + "\nraise SystemExit(1)\n")
    assert run('init').returncode != 0
    assert not (state / 'db').exists()
    swi.write_text(original_script)
    assert run('init').returncode == 0
    key = state / 'admin.key'
    assert key.stat().st_mode & 0o777 == 0o600
    secret = key.read_text().strip()
    assert (state / 'db/digest').read_text() == hashlib.sha256(secret.encode()).hexdigest()
    assert json.loads((state / 'atlas-format.json').read_text()) == {'atlas_format': 3}
    assert run('serve').returncode == 0 and key.read_text().strip() == secret
    # Resume a native init killed after it installed the stage's owner, key
    # and VectorLink directory, but before the database was published.
    import os
    stage = state.rename(tmp_path / 'finished-native-init')
    state.mkdir(mode=0o700)
    stage = stage.rename(state / '.graph-ready-v3')
    os.link(stage / 'atlas-format.json', state / 'atlas-format.json')
    os.link(stage / 'admin.key', key)
    (state / 'vectorlink').mkdir()
    swi.unlink()  # Recovery must use this completed init and its original key.
    assert run('init').returncode == 0
    assert run('serve').returncode == 0 and key.read_text().strip() == secret
    key.unlink()
    before = {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert run('serve').returncode != 0 and run('init').returncode != 0
    assert before == {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()}


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


def startup_config(tmp_path, monkeypatch, *, runtime_check=None):
    module = deployment()
    data = tmp_path / 'data'
    data.mkdir(exist_ok=True)
    profiles = json.loads((PACKAGE / 'runtime/model-profiles.json').read_text())
    (data / 'profiles.json').write_text(json.dumps({k: {**v, 'provider': 'codex'} for k, v in profiles.items()}))
    monkeypatch.setenv('ATLAS_DATA_ROOT', str(data))
    monkeypatch.setenv('ATLAS_RUNTIME_ROOT', str(tmp_path / 'runtime'))
    binary = tmp_path / 'runtime/providers/codex/node_modules/.bin/codex'
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.exists():
        binary.write_text('#!/bin/sh\necho "codex-cli 0.167.0"\n')
        binary.chmod(0o755)
    monkeypatch.setattr(module, 'check_packaged_runtime', runtime_check or (lambda *_: None))
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


@pytest.mark.parametrize('kind', ['single-atlas', 'graph', 'bound-config', 'cache', 'registry'])
def test_docker_rejects_unsupported_storage_before_component_start(tmp_path, monkeypatch, kind):
    import sqlite3
    from knowledge_atlas.contracts import AtlasError
    from knowledge_atlas.core.git_store import git
    from knowledge_atlas.core.publication import Publications
    from test_project_registry import retained_publication
    data = tmp_path / 'data'
    data.mkdir()
    if kind == 'single-atlas':
        _, store, ready = retained_publication(data / 'atlas', database='admin/atlas',
                                              service_root=tmp_path / 'fixture-service')
        refs = git(store.root, 'show-ref')
    elif kind == 'graph':
        (data / 'graph/db').mkdir(parents=True)
        (data / 'graph/db/publication').write_bytes(b'original')
    elif kind == 'cache':
        (data / 'service/vectors').mkdir(parents=True)
        with sqlite3.connect(data / 'service/vectors/vectors.sqlite3') as db:
            db.execute('CREATE TABLE vectors (key TEXT PRIMARY KEY, artifact TEXT NOT NULL)')
    elif kind == 'registry':
        (data / 'service').mkdir()
        with sqlite3.connect(data / 'service/operations.sqlite3') as db:
            db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        (data / 'service/token').write_text('retain original')
    else:
        (data / 'service-config.json').write_text('{"configuration":{"atlas_path":"legacy"}}')
    def files():
        return {p.relative_to(data): p.read_bytes() if p.is_file() else None for p in data.rglob('*')
                if p.name != 'profiles.json'}
    original = files()
    with pytest.raises((ValueError, AtlasError), match='unsupported'):
        startup_config(tmp_path, monkeypatch, runtime_check=lambda *_: pytest.fail('started runtime probes'))
    assert files() == original
    if kind == 'single-atlas':
        assert git(store.root, 'show-ref') == refs and Publications(store).ready() == ready


def test_restart_with_project_in_nested_data_directory(tmp_path, monkeypatch):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project

    config, _ = startup_config(tmp_path, monkeypatch)
    path = tmp_path / 'data/atlas/notes'
    identity = create_project(service_context(config), path)
    restarted, _ = startup_config(tmp_path, monkeypatch)
    binding, = list_projects(service_context(restarted))
    assert (binding.atlas_id, binding.path) == (identity, path)


@pytest.mark.parametrize('mount', ['missing', 'different-repository'])
def test_restart_serves_other_projects_after_completed_binding(tmp_path, monkeypatch, mount):
    from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project

    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'atlas'
    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    identity = create_project(service, path)
    other = create_project(service, data / 'second')
    before = list_projects(service)
    binding = next(project for project in before if project.atlas_id == identity)
    assert binding.graph_database.startswith('admin/atlas-')
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


def compose_service(*files):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI required to resolve deployment recipe')
    command = ['docker', 'compose', '-p', 'atlas-fixture']
    for file in files:
        command.extend(['-f', str(PACKAGE / 'docker' / file)])
    result = subprocess.run([*command, 'config', '--format', 'json'], check=True,
                            env={**os.environ, 'ATLAS_REVISION': '1' * 40,
                                 'ATLAS_VERSION': '0.4.0', 'ATLAS_IMAGE_DIGEST': ''},
                            capture_output=True, text=True)
    return json.loads(result.stdout)['services']['atlas']


def test_default_compose_pulls_only():
    service = compose_service('compose.yaml')
    assert service['image'] == 'ghcr.io/sergesha/knowledge-atlas:0.4.0'
    assert 'build' not in service
    assert {v['target'] for v in service['volumes']} == {'/runtime', '/data', '/auth'}


def test_explicit_build_overlay():
    assert (PACKAGE / 'docker/compose.build.yaml').is_file(), 'local build overlay missing'
    service = compose_service('compose.yaml', 'compose.build.yaml')
    assert service['image'] == 'knowledge-atlas:local'
    assert service['pull_policy'] == 'never'
    assert service['build']['context'] == str(PACKAGE)
    assert service['build']['dockerfile'] == 'docker/Dockerfile'
    assert service['build']['args']['ATLAS_REVISION'] == '1' * 40


@pytest.mark.parametrize(('provider', 'package', 'version'), [
    ('codex', '@openai/codex', '0.167.0'), ('claude', '@anthropic-ai/claude-code', '2.1.319')])
def test_provider_login_acquires_verified_selected_cli(tmp_path, monkeypatch, provider, package, version):
    module = deployment()
    assert hasattr(module, 'ensure_provider'), 'login needs the selected vendor CLI'
    npm = tmp_path / 'npm'
    npm.write_text('#!' + sys.executable + '\n' + """import json, os, pathlib, sys
assert sys.argv[1] == 'ci'
lock = json.loads(pathlib.Path('package-lock.json').read_text())
wrapper = lock['packages']['node_modules/' + os.environ['FIXTURE_PACKAGE']]
assert wrapper['integrity'].startswith('sha512-')
assert wrapper['version'] == os.environ['FIXTURE_VERSION']
binary = pathlib.Path('node_modules/.bin') / os.environ['FIXTURE_PROVIDER']
binary.parent.mkdir(parents=True)
binary.write_text('#!/bin/sh\\necho ' + wrapper['version'] + '\\n')
binary.chmod(0o755)
""")
    npm.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setenv('FIXTURE_PACKAGE', package)
    monkeypatch.setenv('FIXTURE_VERSION', version)
    monkeypatch.setenv('FIXTURE_PROVIDER', provider)
    volume = tmp_path / 'volume'
    volume.mkdir()
    binary = module.ensure_provider(volume, provider)
    assert binary.is_relative_to(volume)
    assert subprocess.check_output([binary, '--version'], text=True).strip() == version
    npm.unlink()
    assert module.ensure_provider(volume, provider) == binary
    assert not (volume / 'providers' / ('claude' if provider == 'codex' else 'codex')).exists()


def test_start_refuses_replaced_provider(tmp_path, monkeypatch):
    binary = tmp_path / 'runtime/providers/codex/node_modules/.bin/codex'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "codex-cli 0.0.1"\n')
    binary.chmod(0o755)
    with pytest.raises(RuntimeError, match='codex 0.167.0'):
        startup_config(tmp_path, monkeypatch)
    assert not (tmp_path / 'data/service-config.json').exists()


@pytest.mark.parametrize('invalid', ['missing-stage', 'unknown-provider', 'missing-model', 'invalid-shape', 'invalid-effort'])
def test_prepare_validates_retained_profiles_before_setup(tmp_path, monkeypatch, invalid):
    module = deployment()
    profiles = {name: {'provider': 'codex', 'model': 'fixture', 'effort': 'high'} for name in
                ('wiki', 'open', 'phrase', 'proposal', 'review_extension', 'grounded_answer')}
    if invalid == 'missing-stage':
        del profiles['wiki']
    elif invalid == 'unknown-provider':
        profiles['open']['provider'] = 'unknown'
    elif invalid == 'missing-model':
        del profiles['phrase']['model']
    elif invalid == 'invalid-effort':
        profiles['wiki']['effort'] = 'unknown'
    else:
        profiles = []
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'profiles.json').write_text(json.dumps(profiles))
    volume = tmp_path / 'volume'
    monkeypatch.setattr(module, 'roots', lambda: (volume, data))
    monkeypatch.setattr(module, 'seed_packaged_runtime', lambda *_: pytest.fail('unexpected runtime setup'))
    monkeypatch.setattr(sys, 'argv', ['entrypoint.py', 'prepare'])
    monkeypatch.setattr(module.os, 'umask', lambda *_: None)
    with pytest.raises(ValueError, match='profiles.json'):
        module.main()
    assert not volume.exists()


def test_provider_boundary_rejects_unrecognized_name_without_writes(tmp_path):
    module = deployment()
    with pytest.raises(ValueError, match='provider'):
        module.ensure_provider(tmp_path, 'unknown')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('anonymous_volume', [False, True], ids=['image', 'fresh-volume'])
def test_image_auth_starts_empty(anonymous_volume):
    image = os.environ.get('ATLAS_TEST_IMAGE')
    if not image:
        pytest.skip('set ATLAS_TEST_IMAGE to the local packaged image under qualification')
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
               '--entrypoint', '/usr/bin/python3']
    if anonymous_volume:
        command.extend(['--volume', '/auth'])
    command.extend([image, '-c', 'import json,os; print(json.dumps(os.listdir("/auth")))'])
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(result.stdout) == []


def test_second_supervisor_cannot_start_processes_on_same_data(tmp_path, monkeypatch):
    import fcntl
    data = tmp_path / 'data'
    data.mkdir()
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match='already supervising'):
            startup_config(tmp_path, monkeypatch, runtime_check=lambda *_: pytest.fail('started runtime probes'))
        assert not (data / 'service').exists() and not (data / 'logs').exists()
    finally:
        os.close(fd)

@pytest.mark.parametrize('mount', ['missing', 'empty'])
def test_restart_refuses_displaced_managed_projection_before_runtime(tmp_path, monkeypatch, mount):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.operations import database
    from knowledge_atlas.core.publication import Publications
    from test_project_registry import retained_publication
    config, _ = startup_config(tmp_path, monkeypatch)
    service = service_context(config)
    data = tmp_path / 'data'
    identity, store, ready = retained_publication(data / 'project', service_root=service.service_root)
    with database(service) as db:
        graph_database = db.execute('SELECT graph_database FROM atlases WHERE atlas_id=?', (identity,)).fetchone()[0]
        db.execute('INSERT INTO projection_owners VALUES (?,?,?)',
                   ('http://127.0.0.1:9403', graph_database, identity))
    graph = data / 'graph'
    if mount == 'empty':
        graph.mkdir()
    config_path = data / 'service-config.json'
    original = config_path.read_bytes()
    registry = service.service_root / 'operations.sqlite3'
    original_registry = registry.read_bytes()
    with pytest.raises(ValueError, match='graph'):
        startup_config(tmp_path, monkeypatch, runtime_check=lambda *_: pytest.fail('started runtime probes'))
    assert not graph.exists() or not list(graph.iterdir())
    assert original == config_path.read_bytes() and original_registry == registry.read_bytes()
    assert Publications(store).ready() == ready


def test_docker_rejects_linked_service_slot_without_following_it(tmp_path, monkeypatch):
    from knowledge_atlas.contracts import AtlasError
    data = tmp_path / 'data'
    data.mkdir()
    target = tmp_path / 'empty-target'
    target.mkdir()
    (data / 'service').symlink_to(target, target_is_directory=True)
    with pytest.raises(AtlasError, match='unsupported'):
        startup_config(tmp_path, monkeypatch, runtime_check=lambda *_: pytest.fail('started runtime probes'))
    assert not (data / 'service-config.json').exists()
    assert (data / 'service').is_symlink() and not list(target.iterdir())
