"""Pinned external runtime identity checks. Validation never acquires components."""
from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from .contracts import RuntimeConfig, RuntimeIdentity


class RuntimeMismatch(RuntimeError):
    def __init__(self, component: str, code: str, detail: str):
        self.component = component
        self.code = code
        super().__init__(f'{component}: {detail}')


def platform_target() -> str:
    pair = (platform.system(), platform.machine().lower())
    targets = {('Darwin', 'arm64'): 'macos-arm64', ('Linux', 'x86_64'): 'linux-x86_64',
               ('Linux', 'aarch64'): 'linux-arm64', ('Linux', 'arm64'): 'linux-arm64'}
    if pair not in targets:
        raise RuntimeMismatch('platform', 'unsupported_platform', '/'.join(pair))
    return targets[pair]


def check_prerequisites() -> dict[str, str]:
    """Inspect existing tools only; never invoke a system package manager."""
    result = {}
    for name in ('git', 'cargo', 'rustc', 'cmake', 'ninja', 'make', 'm4', 'cc', 'c++', 'protoc', 'pkg-config'):
        executable = shutil.which(name)
        if executable is None:
            raise RuntimeMismatch(name, 'missing_prerequisite', f'install {name} before runtime prepare')
        result[name] = executable
    return result


def _assets() -> Path:
    # Source checkout and wheel both retain the runtime resource directory.
    packaged = Path(__file__).parent / '_assets' / 'runtime'
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / 'runtime'


def _json(path: Path, component: str):
    if not path.is_file():
        raise RuntimeMismatch(component, 'missing_component', str(path))
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise RuntimeMismatch(component, 'invalid_manifest', str(exc)) from exc


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _check_hash(path: Path, expected: str, component: str):
    if not path.is_file():
        raise RuntimeMismatch(component, 'missing_component', str(path))
    if _hash(path) != expected:
        raise RuntimeMismatch(component, 'artifact_mismatch', 'SHA-256 does not match pinned identity')


SWI_BROWSER = 'native-tools/swipl/lib/swipl/library/www_browser.pl'


def _required_artifacts(target: str) -> tuple[str, ...]:
    suffix = '.dylib' if target == 'macos-arm64' else '.so'
    return ('terminusdb/terminusdb', 'native-tools/swipl/bin/swipl', 'extract/target/debug/poc9-extract',
            'terminusdb/src/rust/librust' + suffix, 'wiki/mcp/local_server.py',
            'wiki/mcp/vaultfs/sqlite.py', 'chunking.py', SWI_BROWSER)


def _python_packages(root: Path) -> dict[str, str]:
    executable = root / 'python/bin/python'
    if not executable.is_file():
        raise RuntimeMismatch('python/bin/python', 'missing_component', str(executable))
    script = 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}))'
    try:
        result = subprocess.run([str(executable), '-c', script], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeMismatch('python', 'artifact_mismatch', 'cannot inspect native Python environment') from exc


def _source_inventory(root: Path, name: str, directory: str) -> dict:
    path = root / directory
    if not (path / '.git').exists():
        raise RuntimeMismatch(name, 'missing_component', 'pinned source checkout missing')
    try:
        revision = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        filenames = subprocess.check_output(['git', '-C', str(path), 'ls-files', '-z']).decode().split('\0')
        files = {filename: _hash(path / filename) for filename in filenames if filename and (path / filename).is_file()}
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeMismatch(name, 'artifact_mismatch', 'source inventory unavailable') from exc
    return {'revision': revision, 'files': files}


def validate_runtime(config: RuntimeConfig) -> RuntimeIdentity:
    return _validate_runtime(config, check_model=True)


def _validate_runtime(config: RuntimeConfig, *, check_model: bool) -> RuntimeIdentity:
    root = config.runtime_root.expanduser().resolve()
    target = platform_target()
    manifest = _json(config.manifest or root / 'runtime.json', 'manifest')
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise RuntimeMismatch('manifest', 'invalid_manifest', 'schema_version 1 required')
    if manifest.get('platform') != target:
        raise RuntimeMismatch('platform', 'artifact_mismatch', f'expected {target}')
    artifacts = _json(_assets() / 'gemma-runtime-artifacts.json', 'pinned model artifacts')
    if check_model:
        for name, digest in artifacts['artifacts'].items():
            _check_hash(root / 'model' / name, digest, 'model/' + name)
    profile = _json(root / 'embedding-profile.json', 'embedding profile')
    expected = _json(_assets() / 'embedding-profile.json', 'pinned embedding profile')
    if profile != expected:
        raise RuntimeMismatch('embedding profile', 'artifact_mismatch', 'qualified profile required')
    pins = _json(_assets() / 'components.json', 'pinned components')
    components = manifest.get('components', {})
    for name in ('wiki_llm', 'utopia_extract', 'terminusdb', 'vectorlink'):
        if components.get(name) != pins[name]['revision']:
            raise RuntimeMismatch(name, 'artifact_mismatch', 'pinned source revision required')
    required = _required_artifacts(target)
    browser_pin = _json(_assets() / 'patches/manifest.json', 'pinned patches')['swipl']
    _check_hash(root / SWI_BROWSER, browser_pin['after_sha256'], SWI_BROWSER)
    recorded = manifest.get('artifacts', {})
    for name in required:
        digest = recorded.get(name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeMismatch(name, 'missing_component', 'build artifact identity missing')
        _check_hash(root / name, digest, name)
    packages = _python_packages(root)
    for name, version in artifacts['runtime'].items():
        if packages.get(name) != version:
            raise RuntimeMismatch(name, 'artifact_mismatch', 'qualified Python dependency version required')
    if packages != manifest.get('python_packages'):
        raise RuntimeMismatch('python', 'artifact_mismatch', 'installed dependencies changed after preparation')
    for name, directory in (('wiki_llm', 'wiki'), ('terminusdb', 'terminusdb'), ('vectorlink', 'vectorlink')):
        observed = _source_inventory(root, name, directory)
        if observed['revision'] != pins[name]['revision'] or observed != manifest.get('sources', {}).get(name):
            raise RuntimeMismatch(name, 'artifact_mismatch', 'pinned source or prepared patch contents changed')
    return RuntimeIdentity(root, target, components, profile['profile_id'])


def _acquire(url: str, destination: Path, digest: str, *, partial: Path | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = (tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
              if partial is None else partial.open('xb'))
    with output:
        partial = Path(output.name)
        try:
            with urllib.request.urlopen(url, timeout=120) as source:
                shutil.copyfileobj(source, output)
            output.flush()
            _check_hash(partial, digest, destination.name)
            partial.replace(destination)
        except (OSError, ValueError) as exc:
            raise RuntimeMismatch(destination.name, 'acquisition_failed', f'{url}: {exc}') from exc
        finally:
            partial.unlink(missing_ok=True)


def acquire_model(root: Path, *, partial: Path | None = None):
    """Acquire only the pinned weights and tokenizer; completed files can be reused."""
    pins = _json(_assets() / 'components.json', 'pinned components')['embedding']
    model = _json(_assets() / 'gemma-runtime-artifacts.json', 'model artifacts')
    for name, digest in model['artifacts'].items():
        path = root / 'model' / name
        if path.resolve() != path:
            raise RuntimeMismatch(name, 'invalid_location', 'unexpected symlink in model destination')
        if path.exists():
            _check_hash(path, digest, name)
        else:
            url = f"https://huggingface.co/{pins['model']}/resolve/{pins['revision']}/{name}"
            _acquire(url, path, digest, partial=partial)


def prepare_runtime(config: RuntimeConfig) -> RuntimeIdentity:
    """Acquire pinned sources in a fresh external tree and retain failed build logs."""
    return _build_runtime(config)


def _build_runtime(config: RuntimeConfig, *, image_revision: str | None = None) -> RuntimeIdentity:
    import os
    import tarfile

    root = config.runtime_root.expanduser().resolve()
    target = platform_target()
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise RuntimeMismatch('runtime', 'invalid_location', 'runtime must live outside the installed plugin')
    if (config.manifest or root / 'runtime.json').exists():
        return validate_runtime(config)
    if root.exists() and any(root.iterdir()):
        raise RuntimeMismatch('runtime', 'destination_exists', 'use a fresh runtime directory; failed builds are retained')
    tools = check_prerequisites()
    if shutil.which('uv') is None:
        raise RuntimeMismatch('uv', 'missing_prerequisite', 'install uv before runtime prepare')
    root.mkdir(parents=True, exist_ok=True)
    assets = _assets()
    pins = _json(assets / 'components.json', 'pinned components')
    patches = _json(assets / 'patches/manifest.json', 'pinned patches')
    environment = os.environ.copy()
    environment.update(CARGO_HOME=str(root / 'cargo-home'), CARGO_TARGET_DIR=str(root / 'cargo-target'),
                       CARGO_BUILD_JOBS='1', CARGO_PROFILE_DEV_DEBUG='0', CARGO_NET_GIT_FETCH_WITH_CLI='true')
    log = root / 'prepare.log'

    def run(component, argv, cwd=root):
        with log.open('a', encoding='utf-8') as output:
            output.write(json.dumps({'component': component, 'argv': list(map(str, argv))}) + '\n')
            output.flush()
            result = subprocess.run(list(map(str, argv)), cwd=cwd, env=environment, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeMismatch(component, 'build_failed', f'exit {result.returncode}; retained log: {log}')

    def clone(name, url, revision, destination):
        if image_revision:
            # Only the selected commit and its tree belong in a distributed checkout.
            run(name, [tools['git'], 'init', destination])
            run(name, [tools['git'], 'fetch', '--no-tags', '--depth=1', url, revision], destination)
        else:
            run(name, [tools['git'], 'clone', '--no-checkout', url, destination])
        run(name, [tools['git'], 'checkout', '--detach', revision], destination)
        actual = subprocess.check_output([tools['git'], '-C', str(destination), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != revision:
            raise RuntimeMismatch(name, 'artifact_mismatch', 'source checkout differs from pin')

    def apply_patch(name, patch, directory):
        run(name, [tools['git'], 'apply', patch], directory)
        if image_revision:
            for line in patch.read_text().splitlines():
                if line.startswith('+++ b/'):
                    path = directory / line.removeprefix('+++ b/')
                    comment = {'.pl': '%', '.rs': '//', '.lock': '#', '.toml': '#', '.txt': '#'}[path.suffix]
                    with path.open('a') as modified:
                        modified.write(f'\n{comment} Modified by Knowledge Atlas; see runtime/patches/{patch.name}.\n')

    for name, directory in (('wiki_llm', 'wiki'), ('terminusdb', 'terminusdb'), ('vectorlink', 'vectorlink')):
        clone(name, pins[name]['repository'], pins[name]['revision'], root / directory)
    for name, directory in (('wiki_llm', 'wiki'), ('terminusdb', 'terminusdb'), ('vectorlink', 'vectorlink')):
        patch = assets / 'patches' / patches[name]['patch']
        _check_hash(patch, patches[name]['sha256'], name + ' patch')
        apply_patch(name, patch, root / directory)

    swi_archive = root / 'downloads/swipl-10.0.2.tar.gz'
    _acquire('https://www.swi-prolog.org/download/stable/src/swipl-10.0.2.tar.gz', swi_archive,
            'e42cc098f7b8a6051c4f79a99b55162d467098aba60f69649bdc7583f0734b57')
    with tarfile.open(swi_archive) as archive:
        archive.extractall(root / 'sources', filter='data')
    source = root / 'sources/swipl-10.0.2'
    browser = source / 'library/www_browser.pl'
    browser_pin = patches['swipl']
    patch = assets / 'patches' / browser_pin['patch']
    _check_hash(patch, browser_pin['sha256'], 'SWI browser patch')
    _check_hash(browser, browser_pin['before_sha256'], 'SWI browser source')
    run('swipl', [tools['git'], 'apply', patch], source)
    _check_hash(browser, browser_pin['after_sha256'], 'patched SWI browser source')
    swi = root / 'native-tools/swipl'
    run('swipl', [tools['cmake'], '-S', root / 'sources/swipl-10.0.2', '-B', root / 'swipl-build',
                  '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={swi}',
                  '-DSWIPL_PACKAGES_GUI=OFF', '-DSWIPL_PACKAGES_JAVA=OFF', '-DSWIPL_PACKAGES_ODBC=OFF',
                  '-DINSTALL_DOCUMENTATION=OFF', '-DBUILD_TESTING=OFF'])
    run('swipl', [tools['cmake'], '--build', root / 'swipl-build', '--parallel', '4'])
    run('swipl', [tools['cmake'], '--install', root / 'swipl-build'])
    tus = swi / 'lib/swipl/pack/tus'
    tus.parent.mkdir(parents=True, exist_ok=True)
    clone('tus', 'https://github.com/terminusdb-org/tus.git', '033ab8fd46db0b7b29bdfc7f038cde30eac22a1d', tus)
    environment.update(PATH=str(swi / 'bin') + os.pathsep + environment.get('PATH', ''),
                       SWIPL_DIR=str(swi / 'bin') + '/', SWIPL_PACK_PATH=str(tus.parent),
                       TERMINUSDB_GIT_HASH=pins['terminusdb']['revision'], TERMINUSDB_INDEXER_BACKEND='none')
    rust = root / 'terminusdb/src/rust'
    run('terminusdb', [tools['cargo'], 'fetch', '--locked'], rust)
    crates = list((root / 'cargo-home/registry/cache').glob('*/lance-7.0.0.crate'))
    sources = list((root / 'cargo-home/registry/src').glob('*/lance-7.0.0'))
    if len(crates) != 1 or len(sources) != 1:
        raise RuntimeMismatch('lance', 'missing_component', 'expected exactly one locked Lance 7.0.0 crate')
    _check_hash(crates[0], patches['lance']['crate_sha256'], 'lance crate')
    lance_patch = assets / 'patches' / patches['lance']['patch']
    _check_hash(lance_patch, patches['lance']['sha256'], 'lance patch')
    apply_patch('lance', lance_patch, sources[0])
    build_options = (['--config', 'profile.dev.package.lance-bitpacking.codegen-units=1']
                     if target == 'linux-arm64' else [])
    cargo_messages = ['--message-format=json-render-diagnostics'] if image_revision else []
    run('terminusdb', [tools['cargo'], 'build', '--locked', *cargo_messages, '-p', 'terminusdb-dylib', '--lib',
                       *build_options], rust)
    suffix = '.dylib' if target == 'macos-arm64' else '.so'
    shutil.copy2(root / ('cargo-target/debug/libterminusdb_dylib' + suffix), rust / ('librust' + suffix))
    (rust / '.last-dist').write_text('community')
    run('terminusdb', [tools['make'], '-f', 'distribution/Makefile.prolog', 'dev-build'], root / 'terminusdb')
    native = assets.parent / 'native/extract'
    if not native.is_dir():
        native = Path(__file__).resolve().parents[2] / 'native/extract'
    shutil.copytree(native, root / 'extract')
    extract_env = environment.pop('CARGO_TARGET_DIR')
    try:
        run('utopia_extract', [tools['cargo'], 'build', '--locked', *cargo_messages], root / 'extract')
    finally:
        environment['CARGO_TARGET_DIR'] = extract_env
    model = _json(assets / 'gemma-runtime-artifacts.json', 'model artifacts')
    if image_revision is None:
        acquire_model(root)
    shutil.copy2(assets / 'embedding-profile.json', root / 'embedding-profile.json')
    shutil.copy2(assets / 'chunking.py', root / 'chunking.py')
    run('python', ['uv', 'venv', '--python', '3.12.16', root / 'python'])
    python = root / 'python/bin/python'
    run('python', ['uv', 'pip', 'install', '--python', python,
        '-r', root / 'wiki/mcp/requirements.lock',
        *[f'{name}=={version}' for name, version in model['runtime'].items()]])
    required = _required_artifacts(target)
    toolchains = {name: subprocess.check_output([path, '--version'], text=True, stderr=subprocess.STDOUT).splitlines()[0]
                  for name, path in tools.items()}
    manifest = {'schema_version': 1, 'platform': target,
                'components': {name: pins[name]['revision'] for name in ('wiki_llm', 'utopia_extract', 'terminusdb', 'vectorlink')},
                'sources': {name: _source_inventory(root, name, directory)
                            for name, directory in (('wiki_llm', 'wiki'), ('terminusdb', 'terminusdb'), ('vectorlink', 'vectorlink'))},
                'toolchains': toolchains, 'python_packages': _python_packages(root), 'artifacts': {name: _hash(root / name) for name in required}}
    if image_revision:
        from . import __version__
        manifest.update(atlas_version=__version__, source_revision=image_revision)
    (config.manifest or root / 'runtime.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return _validate_runtime(config, check_model=image_revision is None)
