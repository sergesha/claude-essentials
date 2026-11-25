"""Bind a writable runtime volume to native artifacts at their immutable image paths."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re

from . import __version__, runtime
from .contracts import RuntimeConfig, RuntimeIdentity
from .runtime import RuntimeMismatch

# Relative entry points used by the native service. Sources, Python and SWI
# stay at the paths used by their build; only the model and provider CLIs are acquired.
SEED_ENTRIES = ('wiki', 'terminusdb', 'vectorlink', 'extract', 'native-tools',
                'python', 'chunking.py', 'embedding-profile.json', 'runtime.json')


def _markers(seed: Path, destination: Path) -> tuple[Path, Path]:
    path = seed / 'runtime.json'
    manifest = runtime._json(path, 'packaged runtime')
    if not isinstance(manifest, dict):
        raise RuntimeMismatch('packaged runtime', 'invalid_manifest', 'expected an image build manifest')
    if manifest.get('atlas_version') != __version__:
        raise RuntimeMismatch('packaged runtime', 'artifact_mismatch', 'Atlas application version differs')
    if manifest.get('platform') != runtime.platform_target():
        raise RuntimeMismatch('packaged runtime', 'artifact_mismatch', 'image architecture differs')
    revision = manifest.get('source_revision')
    if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
        raise RuntimeMismatch('packaged runtime', 'artifact_mismatch', 'missing exact source revision')
    receipt = '.atlas-seed-' + runtime._hash(path)
    return destination / (receipt + '.pending'), destination / (receipt + '.ready')


def _incompatible():
    return RuntimeMismatch('runtime', 'incompatible_volume',
                           'select a fresh runtime volume; keep existing runtime, data and auth volumes')


def _links(seed: Path, destination: Path, *, initialize: bool):
    for name in SEED_ENTRIES:
        path = destination / name
        target = seed / name
        if path.is_symlink() and path.readlink() == target:
            continue
        if not initialize or path.is_symlink() or path.exists():
            raise _incompatible()
        path.symlink_to(target, target_is_directory=target.is_dir())


def check_packaged_runtime(seed: Path, destination: Path) -> RuntimeIdentity:
    """Read-only startup. Require completion for the exact seed, even on restart."""
    seed, destination = seed.resolve(), destination.resolve()
    _, ready = _markers(seed, destination)
    if ready.is_symlink() or not ready.is_dir():
        raise _incompatible()
    _links(seed, destination, initialize=False)
    return runtime.validate_runtime(RuntimeConfig(destination))


def seed_packaged_runtime(seed: Path, destination: Path) -> RuntimeIdentity:
    seed, destination = seed.resolve(), destination.resolve()
    pending, ready = _markers(seed, destination)
    destination.mkdir(parents=True, exist_ok=True)
    # Lock the mount directory itself. No lockfile is left in an unrelated volume.
    lock = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready.is_dir():
            return check_packaged_runtime(seed, destination)
        if pending.is_symlink() or not pending.is_dir():
            if any(destination.iterdir()):
                raise _incompatible()
            pending.mkdir()
        allowed = {*SEED_ENTRIES, pending.name, 'model'}
        if any(p.name not in allowed for p in destination.iterdir()):
            raise _incompatible()
        _links(seed, destination, initialize=True)
        # The pending marker owns this single staging file. The mount lock also
        # covers recovery after a killed downloader and every subsequent write.
        partial = pending / 'model.download'
        partial.unlink(missing_ok=True)
        runtime.acquire_model(destination, partial=partial)
        identity = runtime.validate_runtime(RuntimeConfig(destination))
        pending.rename(ready)
        return identity
    finally:
        os.close(lock)
