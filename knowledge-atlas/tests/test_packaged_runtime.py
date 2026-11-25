"""Packaged initialization with a small source, interpreter and model fixture."""
import importlib.util
import io
import json
import shutil
import subprocess
import urllib.error

import pytest

from knowledge_atlas import __version__, runtime
from test_runtime import prepared_identity_fixture, install_python_transport_fixture


@pytest.fixture
def seed(prepared_identity_fixture, monkeypatch):
    root, _ = prepared_identity_fixture
    install_python_transport_fixture(root)
    path = root / 'runtime.json'
    manifest = json.loads(path.read_text())
    manifest.update(atlas_version=__version__, source_revision='1' * 40, sources={})
    pins = runtime._assets() / 'components.json'
    components = json.loads(pins.read_text())
    components['embedding'] = {'model': 'example/fixture', 'revision': 'a' * 40}
    for directory, name in [('wiki', 'wiki_llm'), ('terminusdb', 'terminusdb'), ('vectorlink', 'vectorlink')]:
        source = root / directory
        source.mkdir(exist_ok=True)
        git = ['git', '-C', str(source)]
        subprocess.run([*git, 'init', '-q'], check=True)
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, '-c', 'user.name=Fixture', '-c', 'user.email=a@example.invalid',
                        'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        inventory = runtime._source_inventory(root, name, directory)
        manifest['sources'][name] = inventory
        manifest['components'][name] = components[name]['revision'] = inventory['revision']
    pins.write_text(json.dumps(components))
    manifest['artifacts'].pop('model/tokenizer.json')
    path.write_text(json.dumps(manifest))
    shutil.rmtree(root / 'model')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: io.BytesIO(b'model fixture'))
    monkeypatch.setattr(runtime, 'check_prerequisites', lambda: pytest.fail('called compiler path'))
    return root


def package():
    assert importlib.util.find_spec('knowledge_atlas.packaged_runtime'), 'packaged seed initialization missing'
    from knowledge_atlas import packaged_runtime
    return packaged_runtime


def test_empty_volume_seeds_without_compiler(seed, tmp_path, monkeypatch):
    destination = tmp_path / 'volume'
    result = package().seed_packaged_runtime(seed, destination)
    assert result.runtime_root == destination
    assert (destination / 'model/tokenizer.json').read_bytes() == b'model fixture'
    assert (destination / 'python/bin/python').resolve() == (seed / 'python/bin/python').resolve()
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: pytest.fail('unexpected download'))
    assert package().seed_packaged_runtime(seed, destination) == result
    assert package().check_packaged_runtime(seed, destination) == result


def test_interrupted_seed_is_recoverable(seed, tmp_path, monkeypatch):
    destination = tmp_path / 'volume'
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr('urllib.request.urlopen', interrupt)
        with pytest.raises(KeyboardInterrupt):
            package().seed_packaged_runtime(seed, destination)
    with pytest.raises(runtime.RuntimeMismatch):
        package().check_packaged_runtime(seed, destination)
    assert package().seed_packaged_runtime(seed, destination).runtime_root == destination


def test_old_runtime_is_not_silently_reused(seed, tmp_path):
    destination = tmp_path / 'volume'
    package().seed_packaged_runtime(seed, destination)
    path = seed / 'runtime.json'
    old = path.read_text()
    path.write_text(json.dumps({**json.loads(old), 'source_revision': '2' * 40}))
    for operation in (package().check_packaged_runtime, package().seed_packaged_runtime):
        with pytest.raises(runtime.RuntimeMismatch, match='fresh runtime volume'):
            operation(seed, destination)
    path.write_text(old)
    assert package().check_packaged_runtime(seed, destination).runtime_root == destination


def test_nonempty_user_data_is_not_overwritten(seed, tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'runtime.json').write_bytes(b'user data')
    with pytest.raises(runtime.RuntimeMismatch, match='fresh runtime volume'):
        package().seed_packaged_runtime(seed, data)
    assert [p.name for p in data.iterdir()] == ['runtime.json']
    assert (data / 'runtime.json').read_bytes() == b'user data'


def test_model_prerequisite_failure_does_not_compile(seed, tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.URLError('offline')
    monkeypatch.setattr('urllib.request.urlopen', unavailable)
    with pytest.raises(runtime.RuntimeMismatch, match='tokenizer.json.*offline'):
        package().seed_packaged_runtime(seed, tmp_path / 'volume')


def test_seed_rejects_wrong_application_before_touching_volume(seed, tmp_path):
    path = seed / 'runtime.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'atlas_version': 'old'}))
    with pytest.raises(runtime.RuntimeMismatch, match='version'):
        package().seed_packaged_runtime(seed, tmp_path / 'volume')
    assert not (tmp_path / 'volume').exists()


def test_copyleft_crate_corresponding_source_is_preserved(tmp_path):
    import hashlib
    from pathlib import Path
    import runpy

    path = Path(__file__).resolve().parents[1] / 'docker/build-seed.py'
    collect = runpy.run_path(str(path))['collect_cargo_materials']
    cargo = tmp_path / 'cargo'
    source = cargo / 'registry/src/index/rug-1.19.2'
    source.mkdir(parents=True)
    (source / 'Cargo.toml').write_text('[package]\nname="rug"\nversion="1.19.2"\nlicense="LGPL-3.0+"\n')
    (source / 'COPYING').write_text('fixture notice')
    archive = cargo / 'registry/cache/index/rug-1.19.2.crate'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'corresponding original source archive')
    materials = tmp_path / 'distribution'
    materials.mkdir()
    unused = cargo / 'registry/src/index/unbuilt-1.0'
    unused.mkdir()
    (unused / 'Cargo.toml').write_text('[package]\nname="unbuilt"\nversion="1.0"\n')
    log = tmp_path / 'prepare.log'
    log.write_text(json.dumps({'reason': 'compiler-artifact', 'manifest_path': str(source / 'Cargo.toml')}))
    collect(cargo, materials, log)
    assert 'unbuilt' not in (materials / 'cargo-inventory.json').read_text()
    saved, = materials.rglob('*.crate')
    assert saved.read_bytes() == archive.read_bytes()
    assert (materials / 'cargo/registry/src/index/rug-1.19.2/COPYING').read_text() == 'fixture notice'
    entry = json.loads((materials / 'cargo-inventory.json').read_text())['registry/src/index/rug-1.19.2']
    assert entry['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert entry['license'] == 'LGPL-3.0+'


def test_process_death_reclaims_only_interrupted_download(seed, tmp_path):
    import sys
    destination = tmp_path / 'volume'
    child = """
import io, os, sys, urllib.request
from pathlib import Path
from knowledge_atlas import runtime
from knowledge_atlas.packaged_runtime import seed_packaged_runtime

class InterruptedDownload(io.BytesIO):
    def read(self, size=-1):
        if self.tell():
            os._exit(73)
        return super().read()

runtime._assets = lambda: Path(sys.argv[3])
urllib.request.urlopen = lambda *a, **kw: InterruptedDownload(b'x' * 1048576)
seed_packaged_runtime(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    result = subprocess.run([sys.executable, '-c', child, str(seed), str(destination),
                             str(runtime._assets())], timeout=10)
    assert result.returncode == 73
    def partials():
        return [p for p in destination.rglob('*') if p.is_file() and p.stat().st_size == 1048576]
    assert len(partials()) == 1
    retained = destination / 'model/tmp-operator-file'
    retained.write_bytes(b'retain')
    result = package().seed_packaged_runtime(seed, destination)
    assert result.runtime_root == destination
    assert retained.read_bytes() == b'retain'
    assert (destination / 'model/tokenizer.json').read_bytes() == b'model fixture'
    assert partials() == []
