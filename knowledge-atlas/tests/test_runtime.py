"""Reject missing or altered runtime inputs before native processing."""
import json
from pathlib import Path
import pytest
from knowledge_atlas.contracts import RuntimeConfig
from knowledge_atlas.runtime import RuntimeMismatch, validate_runtime, check_prerequisites, platform_target


def test_missing_manifest_identifies_component(tmp_path):
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(tmp_path))
    assert error.value.component == 'manifest'
    assert error.value.code == 'missing_component'


def test_wrong_model_hash_is_reported_before_runtime_launch(tmp_path):
    (tmp_path / 'model/onnx').mkdir(parents=True)
    (tmp_path / 'model/tokenizer.json').write_text('altered tokenizer')
    (tmp_path / 'runtime.json').write_text(json.dumps({'schema_version': 1, 'platform': platform_target(), 'components': {}, 'artifacts': {}}))
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(tmp_path))
    assert error.value.component == 'model/tokenizer.json'
    assert error.value.code == 'artifact_mismatch'


def test_missing_system_prerequisites_do_not_create_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', '')
    with pytest.raises(RuntimeMismatch) as error:
        check_prerequisites()
    assert error.value.code == 'missing_prerequisite'
    assert 'git' in str(error.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(('system', 'machine', 'target'), [('Darwin', 'arm64', 'macos-arm64'), ('Linux', 'x86_64', 'linux-x86_64'), ('Linux', 'aarch64', 'linux-arm64'), ('Linux', 'arm64', 'linux-arm64')])
def test_target_selects_platform_artifacts(monkeypatch, system, machine, target):
    monkeypatch.setattr('platform.system', lambda: system)
    monkeypatch.setattr('platform.machine', lambda: machine)
    assert platform_target() == target


def test_windows_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr('platform.system', lambda: 'Windows')
    monkeypatch.setattr('platform.machine', lambda: 'AMD64')
    with pytest.raises(RuntimeMismatch) as error:
        platform_target()
    assert error.value.code == 'unsupported_platform'


def test_prepare_checks_tools_before_creating_destination(tmp_path, monkeypatch):
    from knowledge_atlas.runtime import prepare_runtime
    monkeypatch.setenv('PATH', '')
    destination = tmp_path / 'new runtime'
    with pytest.raises(RuntimeMismatch) as error:
        prepare_runtime(RuntimeConfig(destination))
    assert error.value.code == 'missing_prerequisite'
    assert not destination.exists()


def test_prepare_refuses_nonempty_unrecognized_destination(tmp_path):
    from knowledge_atlas.runtime import prepare_runtime
    sentinel = tmp_path / 'existing-work'
    sentinel.write_text('keep')
    with pytest.raises(RuntimeMismatch) as error:
        prepare_runtime(RuntimeConfig(tmp_path))
    assert error.value.code == 'destination_exists'
    assert sentinel.read_text() == 'keep'


def test_cli_reports_missing_component_as_structured_error(tmp_path):
    import subprocess
    import sys
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'runtime_root': str(tmp_path / 'absent')}))
    result = subprocess.run([sys.executable, '-m', 'knowledge_atlas.cli', 'runtime', 'check', '--config', str(config)], capture_output=True, text=True)
    assert result.returncode == 1
    failure = json.loads(result.stderr)
    assert failure['code'] == 'missing_component'
    assert failure['component'] == 'manifest'
    assert result.stdout == ''


def test_prepare_refuses_runtime_inside_installed_package(tmp_path, monkeypatch):
    from knowledge_atlas import runtime
    monkeypatch.setattr(runtime, '__file__', str(tmp_path / 'plugin/src/knowledge_atlas/runtime.py'))
    destination = tmp_path / 'plugin/private-runtime'
    with pytest.raises(RuntimeMismatch) as error:
        runtime.prepare_runtime(RuntimeConfig(destination))
    assert error.value.code == 'invalid_location'
    assert not destination.exists()


def test_prepare_rechecks_custom_manifest_instead_of_rebuilding(tmp_path):
    from knowledge_atlas.runtime import prepare_runtime
    manifest = tmp_path / 'custom.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'platform': platform_target(), 'components': {}, 'artifacts': {}}))
    with pytest.raises(RuntimeMismatch) as error:
        prepare_runtime(RuntimeConfig(tmp_path, manifest))
    assert error.value.component == 'model/tokenizer.json'
    assert error.value.code == 'missing_component'


@pytest.fixture
def prepared_identity_fixture(tmp_path, monkeypatch):
    """Small transport fixture; not a native build or qualification claim."""
    import hashlib
    from knowledge_atlas import runtime
    root = tmp_path / 'prepared'
    assets = tmp_path / 'assets'
    root.mkdir(); assets.mkdir()
    digest = lambda data: hashlib.sha256(data).hexdigest()
    model = {'artifacts': {'tokenizer.json': digest(b'model fixture')}, 'runtime': {'onnxruntime': '1.30.0', 'tokenizers': '0.22.2', 'numpy': '2.5.3'}}
    (assets / 'gemma-runtime-artifacts.json').write_text(json.dumps(model))
    profile = {'profile_id': 'fixture-profile'}
    for path in (assets / 'embedding-profile.json', root / 'embedding-profile.json'):
        path.write_text(json.dumps(profile))
    components = {name: 'a' * 40 for name in ('wiki_llm', 'utopia_extract', 'terminusdb', 'vectorlink')}
    (assets / 'components.json').write_text(json.dumps({name: {'revision': value} for name, value in components.items()}))
    suffix = '.dylib' if platform_target() == 'macos-arm64' else '.so'
    (assets / 'patches').mkdir()
    (assets / 'patches/manifest.json').write_text(json.dumps({'swipl': {'after_sha256': digest(b'patched browser')}}))
    files = {runtime.SWI_BROWSER: b'patched browser', 'model/tokenizer.json': b'model fixture', 'terminusdb/terminusdb': b'entrypoint',
             'native-tools/swipl/bin/swipl': b'swipl', 'extract/target/debug/poc9-extract': b'extractor',
             'terminusdb/src/rust/librust' + suffix: b'patched library',
             'wiki/mcp/local_server.py': b'wiki fixture', 'wiki/mcp/vaultfs/sqlite.py': b'vault fixture',
             'chunking.py': b'formatter fixture'}
    for name, content in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    manifest = {'schema_version': 1, 'platform': platform_target(), 'components': components,
                'artifacts': {name: digest(value) for name, value in files.items()}}
    (root / 'runtime.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(runtime, '_assets', lambda: assets)
    return root, suffix


def test_replaced_native_library_cannot_validate(prepared_identity_fixture):
    root, suffix = prepared_identity_fixture
    (root / ('terminusdb/src/rust/librust' + suffix)).write_bytes(b'wrong native library')
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == 'terminusdb/src/rust/librust' + suffix
    assert error.value.code == 'artifact_mismatch'


def test_removed_wiki_cannot_validate(prepared_identity_fixture):
    root, _ = prepared_identity_fixture
    (root / 'wiki/mcp/local_server.py').unlink()
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == 'wiki/mcp/local_server.py'
    assert error.value.code == 'missing_component'


def test_missing_python_environment_cannot_validate(prepared_identity_fixture):
    root, _ = prepared_identity_fixture
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == 'python/bin/python'
    assert error.value.code == 'missing_component'


def install_python_transport_fixture(root, versions=None):
    versions = versions or {'onnxruntime': '1.30.0', 'tokenizers': '0.22.2', 'numpy': '2.5.3'}
    executable = root / 'python/bin/python'
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(versions) + "'\n")
    executable.chmod(0o700)
    path = root / 'runtime.json'
    manifest = json.loads(path.read_text()); manifest['python_packages'] = versions
    path.write_text(json.dumps(manifest))


def test_wrong_installed_python_dependency_is_explicit(prepared_identity_fixture):
    root, _ = prepared_identity_fixture
    install_python_transport_fixture(root, {'onnxruntime': 'different', 'tokenizers': '0.22.2', 'numpy': '2.5.3'})
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == 'onnxruntime'
    assert error.value.code == 'artifact_mismatch'


def test_component_labels_without_source_identity_do_not_validate(prepared_identity_fixture):
    root, _ = prepared_identity_fixture
    install_python_transport_fixture(root)
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == 'wiki_llm'
    assert error.value.code == 'missing_component'


def test_missing_m4_is_reported_as_prerequisite(monkeypatch):
    from knowledge_atlas import runtime
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: None if name == 'm4' else '/available/' + name)
    with pytest.raises(RuntimeMismatch) as error:
        runtime.check_prerequisites()
    assert error.value.component == 'm4'
    assert error.value.code == 'missing_prerequisite'


def test_native_prepare_without_docker(tmp_path, monkeypatch, capsys):
    """Reach native preflight directly from the CLI with Docker absent."""
    import sys
    from knowledge_atlas import cli
    config = tmp_path / 'native.json'
    config.write_text(json.dumps({'runtime_root': str(tmp_path / 'runtime')}))
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(sys, 'argv', ['knowledge-atlas', 'runtime', 'prepare', '--config', str(config)])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().err)['component'] == 'git'
    assert not (tmp_path / 'runtime').exists()

def test_self_recorded_old_swi_browser_rejected(prepared_identity_fixture):
    from knowledge_atlas import runtime
    root, _ = prepared_identity_fixture
    (root / runtime.SWI_BROWSER).write_bytes(b'old browser')
    manifest = json.loads((root / 'runtime.json').read_text())
    manifest['artifacts'][runtime.SWI_BROWSER] = runtime._hash(root / runtime.SWI_BROWSER)
    (root / 'runtime.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeMismatch) as error:
        validate_runtime(RuntimeConfig(root))
    assert error.value.component == runtime.SWI_BROWSER
