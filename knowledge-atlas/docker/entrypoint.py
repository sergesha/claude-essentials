#!/usr/bin/env python3
"""Supervise the existing local Atlas components inside one container."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from knowledge_atlas.packaged_runtime import check_packaged_runtime, seed_packaged_runtime
from knowledge_atlas.http_api import write_private
from knowledge_atlas.client import AtlasClient
from knowledge_atlas.launcher import service_context
from knowledge_atlas.project_registry import adopt_existing_atlas
from knowledge_atlas.providers.base import ProviderError
from knowledge_atlas.providers.config import OPERATIONS, resolve_profile

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


SEED = Path('/opt/atlas-seed')
PROVIDERS = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code'}


def ensure_provider(runtime, name):
    """Install the selected vendor binary into this deployment using its integrity lock."""
    if not isinstance(name, str) or name not in PROVIDERS:
        raise ValueError('unsupported provider; choose codex or claude')
    recipe = PACKAGE / 'docker/providers' / name
    providers = runtime / 'providers'
    providers.mkdir(exist_ok=True)
    prefix = providers / name
    relative = Path('node_modules/.bin') / name
    with (providers / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not prefix.exists():
            with tempfile.TemporaryDirectory(dir=providers) as temporary:
                staging = Path(temporary) / 'install'
                shutil.copytree(recipe, staging)
                subprocess.run(['npm', 'ci', '--include=optional', '--no-audit', '--no-fund',
                                '--cache', str(Path(temporary) / 'cache')], cwd=staging, check=True)
                verify_provider(staging / relative, name)
                staging.rename(prefix)
        else:
            verify_provider(prefix / relative, name)
    return prefix / relative


def verify_provider(path, name):
    recipe = PACKAGE / 'docker/providers' / name / 'package.json'
    version = json.loads(recipe.read_text())['dependencies'][PROVIDERS[name]]
    if path.is_file() and version in subprocess.check_output([path, '--version'], text=True).split():
        return
    raise RuntimeError(f'{name} {version} is required; run prepare with the configured profiles or use a fresh runtime volume')


def read_profiles(path):
    profiles = json.loads(path.read_text())
    if not isinstance(profiles, dict) or set(profiles) != set(OPERATIONS):
        raise ValueError('profiles.json requires six explicit provider/model stage profiles')
    for stage, profile in profiles.items():
        if not isinstance(profile, dict) or 'provider' not in profile:
            raise ValueError(f'profiles.json requires an explicit provider for {stage}')
        try:
            resolve_profile(profiles, stage)
        except (ProviderError, TypeError) as exc:
            raise ValueError(f'profiles.json contains invalid settings for {stage}: {exc}') from exc
    return profiles


def start():
    runtime, data = roots()
    check_packaged_runtime(SEED, runtime)
    profiles = read_profiles(data / 'profiles.json')
    for name in sorted({profile['provider'] for profile in profiles.values()}):
        verify_provider(runtime / 'providers' / name / 'node_modules/.bin' / name, name)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {'service_root': str(data / 'service'), 'runtime': {'runtime_root': str(runtime)},
              'configuration': {'project_data_root': str(data), 'profiles': profiles, 'graph': {'url': 'http://127.0.0.1:9403'}}}
    service = service_context(config)
    adopt_existing_atlas(service, data / 'atlas', 'admin/atlas')
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
            '--profile', profile, '--cache', service.service_root / 'vectors', '--endpoint', 'http://127.0.0.1:9401', '--port', '9402'], env)
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
        print('Knowledge Atlas ready', flush=True)
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
        runtime, data = roots()
        path = data / 'profiles.json'
        profiles = read_profiles(path) if path.exists() else {}
        identity = seed_packaged_runtime(SEED, runtime)
        for name in sorted({p['provider'] for p in profiles.values()}):
            ensure_provider(runtime, name)
        print(identity)
    elif args[0] == 'exec' and len(args) > 1:
        if args[1] in PROVIDERS:
            runtime, _ = roots()
            check_packaged_runtime(SEED, runtime)
            binary = ensure_provider(runtime, args[1])
            os.execv(binary, args[1:])
        os.execvp(args[1], args[1:])
    else:
        raise SystemExit('usage: entrypoint.py [start|prepare|exec COMMAND ...]')


if __name__ == '__main__':
    main()
