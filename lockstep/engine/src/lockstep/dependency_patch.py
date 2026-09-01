"""Fail-closed installer for Lockstep's reviewed yamlgraph source patch.

This module is intentionally standard-library only.  It locates distributions
through metadata, never imports yamlgraph, and is safe to use before any
Lockstep runtime module is imported.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable


_ASSET_ROOT = Path(__file__).parent / "_dependency_patches" / "yamlgraph"
_MANIFEST_PATH = _ASSET_ROOT / "manifest.json"
_PATCH_PATH = _ASSET_ROOT / "0.5.22-subgraph-config.patch"
_MANIFEST_FIELDS = {
    "schema",
    "distribution",
    "version",
    "upstream_repository",
    "upstream_issue",
    "upstream_patch_comment",
    "upstream_issue_body_sha256",
    "upstream_patch_sha256",
    "patch_sha256",
    "files",
}
_FILE_FIELDS = {"path", "before_sha256", "after_sha256"}
_FALLBACK_CAPABILITY_PROBE = (
    "from lockstep.recipe.yamlgraph_adapter import probe_native_capabilities; "
    "probe_native_capabilities()"
)


class DependencyPatchError(RuntimeError):
    """The installed dependency cannot be proven safe to use or patch."""


@dataclass(frozen=True)
class PatchResult:
    status: str
    distribution: str
    version: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DependencyPatchError(f"unsafe dependency patch path: {value!r}")
    return path


def _load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyPatchError(f"invalid dependency patch manifest: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise DependencyPatchError("dependency patch manifest has unknown or missing fields")
    if manifest["schema"] != 1 or not isinstance(manifest["files"], list):
        raise DependencyPatchError("unsupported dependency patch manifest schema")
    paths: list[str] = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            raise DependencyPatchError("dependency patch manifest file entry is not closed")
        paths.append(str(_safe_relative_path(item["path"])))
        for key in ("before_sha256", "after_sha256"):
            value = item[key]
            if not isinstance(value, str) or len(value) != 64:
                raise DependencyPatchError(f"invalid {key} for {item['path']}")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise DependencyPatchError("dependency patch manifest paths must be unique and sorted")
    return manifest


def _diff_paths(patch: bytes) -> tuple[str, ...]:
    paths: list[str] = []
    for raw_line in patch.splitlines():
        if not raw_line.startswith(b"diff --git "):
            continue
        try:
            line = raw_line.decode("utf-8")
            left, right = line.removeprefix("diff --git ").split(" ", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise DependencyPatchError("malformed dependency patch diff header") from exc
        if not left.startswith("a/") or not right.startswith("b/"):
            raise DependencyPatchError("dependency patch diff paths must use a/ and b/")
        left_path = str(_safe_relative_path(left[2:]))
        right_path = str(_safe_relative_path(right[2:]))
        if left_path != right_path:
            raise DependencyPatchError("dependency patch may not rename files")
        paths.append(left_path)
    if not paths or len(paths) != len(set(paths)):
        raise DependencyPatchError("dependency patch contains no files or duplicate file diffs")
    return tuple(paths)


def _validate_patch(manifest: dict, patch_path: Path) -> bytes:
    try:
        patch = patch_path.read_bytes()
    except OSError as exc:
        raise DependencyPatchError(f"cannot read dependency patch: {exc}") from exc
    if _sha256_bytes(patch) != manifest["patch_sha256"]:
        raise DependencyPatchError("dependency patch digest mismatch")
    diff_paths = _diff_paths(patch)
    manifest_paths = tuple(item["path"] for item in manifest["files"])
    if diff_paths != manifest_paths:
        raise DependencyPatchError(
            "dependency patch diff does not name exactly the safe manifest paths"
        )
    return patch


def _distribution_root(distribution: importlib.metadata.Distribution) -> Path:
    root = Path(distribution.locate_file("")).resolve()
    if not root.is_dir():
        raise DependencyPatchError(f"distribution root is not a directory: {root}")
    return root


def _targets(
    distribution: importlib.metadata.Distribution, manifest: dict
) -> tuple[tuple[dict, Path], ...]:
    root = _distribution_root(distribution)
    targets: list[tuple[dict, Path]] = []
    for item in manifest["files"]:
        path = Path(distribution.locate_file(item["path"]))
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise DependencyPatchError(
                f"dependency patch target escapes or is missing: {item['path']}"
            ) from exc
        if path.is_symlink() or not resolved.is_file():
            raise DependencyPatchError(
                f"dependency patch target must be a contained regular file: {item['path']}"
            )
        targets.append((item, resolved))
    return tuple(targets)


def _state(distribution: importlib.metadata.Distribution, manifest: dict) -> str:
    categories: list[str] = []
    for item, path in _targets(distribution, manifest):
        digest = _sha256_path(path)
        if digest == item["before_sha256"]:
            categories.append("before")
        elif digest == item["after_sha256"]:
            categories.append("after")
        else:
            categories.append("unknown")
    if set(categories) == {"before"}:
        return "original"
    if set(categories) == {"after"}:
        return "fully patched"
    if "unknown" in categories:
        return "unknown"
    minority = min(categories.count("before"), categories.count("after"))
    return "partial" if minority == 1 else "mixed"


def _version_tuple(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _default_capability_probe(python: str) -> bool:
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe_yamlgraph_native.py"
    command = [python, str(script), "--quiet"] if script.is_file() else [
        python,
        "-c",
        _FALLBACK_CAPABILITY_PROBE,
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _check_version(
    distribution: importlib.metadata.Distribution,
    manifest: dict,
    capability_probe: Callable[[str], bool] | None,
) -> None:
    installed = distribution.version
    expected = manifest["version"]
    if installed == expected:
        return
    installed_tuple = _version_tuple(installed)
    expected_tuple = _version_tuple(expected)
    if installed_tuple is not None and expected_tuple is not None and installed_tuple > expected_tuple:
        probe = capability_probe or _default_capability_probe
        if probe(sys.executable):
            raise DependencyPatchError(
                f"patch obsolete: {manifest['distribution']} {installed} passes native capabilities"
            )
        raise DependencyPatchError(
            f"patch diverged: {manifest['distribution']} {installed} requires owner review"
        )
    raise DependencyPatchError(
        f"wrong version: expected {manifest['distribution']}=={expected}, found {installed}"
    )


def _locate_distribution(name: str) -> importlib.metadata.Distribution:
    try:
        return importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise DependencyPatchError(f"required distribution is not installed: {name}") from exc


def verify_dependency_patch(
    *,
    distribution: importlib.metadata.Distribution | None = None,
    manifest_path: Path = _MANIFEST_PATH,
    patch_path: Path = _PATCH_PATH,
) -> PatchResult:
    """Read-only verification used by ordinary Lockstep startup."""
    manifest = _load_manifest(Path(manifest_path))
    _validate_patch(manifest, Path(patch_path))
    dist = distribution or _locate_distribution(manifest["distribution"])
    _check_version(dist, manifest, None)
    state = _state(dist, manifest)
    if state != "fully patched":
        raise DependencyPatchError(
            f"{manifest['distribution']} {dist.version} dependency patch state is {state}"
        )
    return PatchResult("fully patched", manifest["distribution"], dist.version)


def _git_apply(staging: Path, patch_path: Path) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for suffix in (["--check", str(patch_path)], [str(patch_path)]):
        completed = subprocess.run(
            ["git", "apply", "--no-index", *suffix],
            cwd=staging,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            diagnostic = (completed.stderr or completed.stdout).strip()
            raise DependencyPatchError(f"git apply failed: {diagnostic}")


def _atomic_write_from(source: Path, destination: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.lockstep-", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_rollback(
    targets: tuple[tuple[dict, Path], ...],
    staging: Path,
    final_state: Callable[[], str],
) -> None:
    originals: dict[Path, bytes] = {}
    for item, path in targets:
        original = path.read_bytes()
        if _sha256_bytes(original) != item["before_sha256"]:
            raise DependencyPatchError(
                f"dependency changed before replacement: {item['path']}"
            )
        originals[path] = original
    replaced: list[Path] = []
    try:
        for item, destination in targets:
            _atomic_write_from(staging / item["path"], destination)
            replaced.append(destination)
            if _sha256_path(destination) != item["after_sha256"]:
                raise DependencyPatchError(
                    f"patched output changed during replace: {item['path']}"
                )
        for item, destination in targets:
            if _sha256_path(destination) != item["after_sha256"]:
                raise DependencyPatchError(
                    f"patched output changed during replace: {item['path']}"
                )
        verified_state = final_state()
        if verified_state != "fully patched":
            raise DependencyPatchError(
                f"dependency patch verification failed after replace: {verified_state}"
            )
    except Exception as exc:
        restore_errors: list[str] = []
        for destination in reversed(replaced):
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.lockstep-restore-", dir=destination.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                temporary.write_bytes(originals[destination])
                os.replace(temporary, destination)
            except Exception as restore_exc:  # noqa: BLE001 - retain every diagnostic
                restore_errors.append(f"{destination}: {restore_exc}")
            finally:
                temporary.unlink(missing_ok=True)
        detail = f"; restore errors: {restore_errors}" if restore_errors else ""
        raise DependencyPatchError(f"dependency patch replacement failed: {exc}{detail}") from exc


def apply_dependency_patch(
    *,
    distribution: importlib.metadata.Distribution | None = None,
    manifest_path: Path = _MANIFEST_PATH,
    patch_path: Path = _PATCH_PATH,
    capability_probe: Callable[[str], bool] | None = None,
) -> PatchResult:
    """Apply the reviewed patch exactly once to the located distribution."""
    manifest = _load_manifest(Path(manifest_path))
    _validate_patch(manifest, Path(patch_path))
    dist = distribution or _locate_distribution(manifest["distribution"])
    _check_version(dist, manifest, capability_probe)
    state = _state(dist, manifest)
    if state == "fully patched":
        return PatchResult("already patched", manifest["distribution"], dist.version)
    if state != "original":
        raise DependencyPatchError(
            f"{manifest['distribution']} {dist.version} dependency patch state is {state}"
        )

    targets = _targets(dist, manifest)
    with tempfile.TemporaryDirectory(prefix="lockstep-dependency-patch-") as temporary:
        staging = Path(temporary)
        for item, source in targets:
            destination = staging / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        _git_apply(staging, Path(patch_path).resolve())
        for item, _ in targets:
            if _sha256_path(staging / item["path"]) != item["after_sha256"]:
                raise DependencyPatchError(
                    f"patched output digest mismatch: {item['path']}"
                )
        _replace_with_rollback(
            targets,
            staging,
            lambda: _state(dist, manifest),
        )

    return PatchResult("patched", manifest["distribution"], dist.version)


def main() -> int:
    try:
        result = apply_dependency_patch()
    except DependencyPatchError as exc:
        print(f"Lockstep dependency patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"{result.distribution} {result.version}: {result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
