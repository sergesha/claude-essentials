#!/usr/bin/env python3
"""Supervise the existing local Atlas components inside one container."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from knowledge_atlas.contracts import RuntimeConfig
from knowledge_atlas.runtime import prepare_runtime, validate_runtime
from knowledge_atlas.http_api import write_private
from knowledge_atlas.client import AtlasClient

PACKAGE = Path(__file__).resolve().parents[1]


class Supervisor:
    def __init__(self, logs):
        self.logs = Path(logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.children = []
        self.streams = []

    def launch(self, name, argv, env=None):
        stream = (self.logs / (name + '.log')).open('ab')
        self.streams.append(stream)
        process = subprocess.Popen(list(map(str, argv)), env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append((name, process))
        return process

    def check(self):
        for name, process in self.children:
            if process.poll() is not None:
                raise RuntimeError(f'{name} exited {process.returncode}; inspect {self.logs}')

    def ready(self, process, endpoint, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                with urllib.request.urlopen(endpoint, timeout=1):
                    return
            except OSError:
                time.sleep(.2)
        raise RuntimeError(f'readiness timeout: {endpoint}; inspect {self.logs}')

    def close(self):
        for _, process in reversed(self.children):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for _, process in reversed(self.children):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for stream in self.streams:
            stream.close()


def needs_graph_init(state):
    return not (Path(state) / 'db').exists()


def roots():
    return (Path(os.environ.get('ATLAS_RUNTIME_ROOT', '/runtime')),
            Path(os.environ.get('ATLAS_DATA_ROOT', '/data')))


def migrate_runtime_interpreter(runtime, install_dir):
    """Rebind a 0.3.0 uv venv to the same interpreter build in the image."""
    if not install_dir:
        return
    venv = runtime / 'python'
    cfg = venv / 'pyvenv.cfg'
    legacy = Path('/auth/.local/share/uv/python')
    if not cfg.is_file():
        return
    config = cfg.read_text()
    # Repair links first; a retry after interruption can still read the old cfg.
    for link in (venv / 'bin').glob('python*'):
        if not link.is_symlink():
            continue
        target = link.readlink()
        if target.is_relative_to(legacy):
            new_target = Path(install_dir) / target.relative_to(legacy)
            if not new_target.is_file():
                raise RuntimeError('replacement image lacks the prepared Python interpreter build')
            with tempfile.TemporaryDirectory(dir=link.parent) as tmp:
                replacement = Path(tmp) / 'python'
                replacement.symlink_to(new_target)
                os.replace(replacement, link)
    if str(legacy) + '/' in config:
        with tempfile.NamedTemporaryFile(mode='w', dir=venv, delete=False) as tmp:
            tmp.write(config.replace(str(legacy) + '/', str(install_dir) + '/'))
        os.replace(tmp.name, cfg)


def start():
    runtime, data = roots()
    migrate_runtime_interpreter(runtime, os.environ.get('UV_PYTHON_INSTALL_DIR'))
    validate_runtime(RuntimeConfig(runtime_root=runtime))
    profiles = json.loads((data / 'profiles.json').read_text())
    stages = {'wiki', 'open', 'phrase', 'proposal', 'review_extension', 'grounded_answer'}
    if set(profiles) != stages or any(not isinstance(p, dict) or p.get('provider') not in {'codex', 'claude'}
                                    or not p.get('model') for p in profiles.values()):
        raise ValueError('profiles.json requires six explicit provider/model stage profiles')
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {'service_root': str(data / 'service'), 'runtime': {'runtime_root': str(runtime)},
              'configuration': {'atlas_path': str(data / 'atlas'), 'profiles': profiles, 'graph': {'url': 'http://127.0.0.1:9403', 'database': 'admin/atlas'}}}
    write_private(data / 'service-config.json', json.dumps(config))
    python = runtime / 'python/bin/python'
    profile = PACKAGE / 'runtime/embedding-profile.json'
    env = {**os.environ, 'PYTHONPATH': str(PACKAGE / 'src'),
           'KNOWLEDGE_ATLAS_GRAPH_KEY_FILE': str(data / 'graph/admin.key'),
           'EMBED_MODEL_PATH': str(runtime / 'model'), 'EMBED_PORT': '9401'}
    supervisor = Supervisor(data / 'logs')
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        raw = supervisor.launch('gemma', [python, PACKAGE / 'runtime/gemma-service.py'], env)
        supervisor.ready(raw, 'http://127.0.0.1:9401/health')
        bridge = supervisor.launch('bridge', [python, PACKAGE / 'runtime/embedding-provider.py',
            '--profile', profile, '--cache', data / 'atlas/vectors', '--endpoint', 'http://127.0.0.1:9401', '--port', '9402'], env)
        supervisor.ready(bridge, 'http://127.0.0.1:9402/stats')
        graph = [python, PACKAGE / 'runtime/native-runtime.py', '--runtime', runtime,
                 '--state', data / 'graph', '--profile', profile, '--tokenizer', runtime / 'model/tokenizer.json',
                 '--provider', 'http://127.0.0.1:9402', '--port', '9403']
        if needs_graph_init(data / 'graph'):
            init = supervisor.launch('graph-init', graph + ['init'], env)
            if init.wait(timeout=90):
                raise RuntimeError('graph-init failed; inspect logs')
            supervisor.children = [(n, p) for n, p in supervisor.children if p is not init]
        graph_process = supervisor.launch('graph', graph + ['serve'], env)
        supervisor.ready(graph_process, 'http://127.0.0.1:9403/api/v1/health')
        supervisor.launch('service', [sys.executable, '-m', 'knowledge_atlas.cli', 'serve',
                                    '--config', data / 'service-config.json'], env)
        deadline = time.monotonic() + 30
        while True:
            supervisor.check()
            try:
                AtlasClient(data / 'service').request('/v1/status')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Atlas service readiness timeout; inspect logs')
                time.sleep(.2)
        print('Knowledge Atlas ready; atlas path: ' + str(data / 'atlas'), flush=True)
        while True:
            supervisor.check()
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.close()


def main():
    os.umask(0o077)
    args = sys.argv[1:] or ['start']
    if args == ['start']:
        start()
    elif args == ['prepare']:
        runtime, _ = roots()
        migrate_runtime_interpreter(runtime, os.environ.get('UV_PYTHON_INSTALL_DIR'))
        print(prepare_runtime(RuntimeConfig(runtime_root=runtime)))
    elif args[0] == 'exec' and len(args) > 1:
        os.execvp(args[1], args[1:])
    else:
        raise SystemExit('usage: entrypoint.py [start|prepare|exec COMMAND ...]')


if __name__ == '__main__':
    main()
