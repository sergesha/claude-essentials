"""Build the immutable ARM64 image payload using the native preparation recipe."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from knowledge_atlas import runtime
from knowledge_atlas.contracts import RuntimeConfig


def collect_cargo_materials(cargo: Path, materials: Path, build_log: Path):
    compiled = set()
    with build_log.open() as log:
        for line in log:
            if not line.startswith('{'):
                continue
            message = json.loads(line)
            if message.get('reason') != 'compiler-artifact':
                continue
            manifest = Path(message['manifest_path'])
            if manifest.is_relative_to(cargo):
                relative = manifest.relative_to(cargo)
                if relative.parts[:2] not in {('registry', 'src'), ('git', 'checkouts')}:
                    raise ValueError(f'unknown Cargo dependency source: {relative}')
                compiled.add(cargo.joinpath(*relative.parts[:4]))
    if not compiled:
        raise ValueError('no compiled dependency inventory in native build log')
    provenance = {}
    for source in sorted(compiled):
        relative = source.relative_to(cargo)
        inventory = {}
        for directory, folders, files in os.walk(source):
            folders[:] = [folder for folder in folders if folder not in {'.git', 'target'}]
            for name in files:
                if not name.lower().startswith(('license', 'licence', 'notice', 'copyright', 'copying', 'authors')):
                    continue
                path = Path(directory) / name
                if not path.is_file():
                    continue
                target = materials / 'cargo' / path.relative_to(cargo)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                inventory[str(path.relative_to(source))] = runtime._hash(target)
        if relative.parts[0] == 'git':
            revision = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
            identity = {'revision': revision}
        else:
            package = tomllib.loads((source / 'Cargo.toml').read_text())['package']
            archive = cargo / 'registry/cache' / relative.parts[-2] / (source.name + '.crate')
            license_id = package.get('license', '')
            identity = {'license': license_id, 'sha256': runtime._hash(archive),
                        'repository': package.get('repository')}
            if any(identifier in license_id for identifier in ('GPL', 'MPL', 'CDDL', 'EUPL', 'EPL')):
                sources = materials / 'corresponding-source'
                sources.mkdir(exist_ok=True)
                shutil.copy2(archive, sources / archive.name)
        provenance[str(relative)] = {**identity, 'notices': inventory}
    (materials / 'cargo-inventory.json').write_text(json.dumps(provenance, indent=2))


def build():
    root = Path('/opt/atlas-seed')
    revision = os.environ['ATLAS_REVISION']
    if not re.fullmatch('[0-9a-f]{40}', revision) or runtime.platform_target() != 'linux-arm64':
        raise SystemExit('the image build needs a full Git commit and a native Linux ARM64 builder')
    runtime._build_runtime(RuntimeConfig(root), image_revision=revision)

    # Retain the sources and legal materials associated with the shipped binaries.
    # Keep the build trees, Cargo caches and historical Git objects in the builder.
    materials = Path('/opt/atlas-distribution')
    materials.mkdir()
    shutil.copy2(root / 'downloads/swipl-10.0.2.tar.gz', materials)
    cargo = root / 'cargo-home'
    collect_cargo_materials(cargo, materials, root / 'prepare.log')
    lance, = cargo.glob('registry/src/*/lance-7.0.0')
    shutil.copytree(lance, materials / 'lance-7.0.0')
    # tus is loaded from its installed pack directory; Git metadata is not required there.
    shutil.rmtree(root / 'native-tools/swipl/lib/swipl/pack/tus/.git')
    extractor = root / 'extract/target/debug/poc9-extract'
    extractor.rename(root / 'poc9-extract')
    shutil.rmtree(root / 'extract/target')
    extractor.parent.mkdir(parents=True)
    (root / 'poc9-extract').rename(extractor)
    for name in ('downloads', 'sources', 'swipl-build', 'cargo-home', 'cargo-target'):
        shutil.rmtree(root / name)
    (root / 'prepare.log').unlink()


if __name__ == '__main__':
    build()
