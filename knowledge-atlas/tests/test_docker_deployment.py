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
