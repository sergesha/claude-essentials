from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lockstep import dependency_patch as dp


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_paths() -> tuple[Path, Path]:
    root = Path(dp.__file__).parent / "_dependency_patches" / "yamlgraph"
    return root / "manifest.json", root / "0.5.22-subgraph-config.patch"


def _copy_official_distribution(tmp_path: Path) -> tuple[Path, importlib.metadata.Distribution]:
    source = importlib.metadata.distribution("yamlgraph")
    assert source.version == "0.5.22"
    site = tmp_path / "site packages"
    site.mkdir()
    package = Path(source.locate_file("yamlgraph"))
    shutil.copytree(package, site / "yamlgraph")
    metadata_dir = next(
        Path(source.locate_file(path))
        for path in source.files or ()
        if str(path).endswith(".dist-info/METADATA")
    ).parent
    shutil.copytree(metadata_dir, site / metadata_dir.name)
    (site / "outside.txt").write_text("untouched")
    copied = next(importlib.metadata.distributions(path=[str(site)]))
    assert copied.metadata["Name"] == "yamlgraph"
    manifest = _manifest()
    copied_hashes = {
        item["path"]: _sha256(Path(copied.locate_file(item["path"])))
        for item in manifest["files"]
    }
    after = {item["path"]: item["after_sha256"] for item in manifest["files"]}
    if copied_hashes == after:
        _, patch_path = _canonical_paths()
        subprocess.run(
            ["git", "apply", "--reverse", "--no-index", str(patch_path)],
            cwd=site,
            check=True,
            capture_output=True,
            text=True,
        )
    asset_root = _canonical_paths()[0].parent
    join_manifest = json.loads((asset_root / "native-join-manifest.json").read_text())
    if all(
        _sha256(Path(copied.locate_file(item["path"]))) == item["after_sha256"]
        for item in join_manifest["files"]
    ):
        subprocess.run(
            ["git", "apply", "--reverse", "--no-index", str(asset_root / "0.5.22-native-join.patch")],
            cwd=site, check=True, capture_output=True, text=True,
        )
    return site, copied


def _manifest() -> dict:
    manifest, _ = _canonical_paths()
    return json.loads(manifest.read_text())


def _states(dist: importlib.metadata.Distribution) -> dict[str, str]:
    return {
        item["path"]: _sha256(Path(dist.locate_file(item["path"])))
        for item in _manifest()["files"]
    }


def _distribution_in_state(tmp_path: Path, state: str) -> Path:
    site, dist = _copy_official_distribution(tmp_path)
    files = _manifest()["files"]
    originals = {
        item["path"]: Path(dist.locate_file(item["path"])).read_bytes()
        for item in files
    }
    if state in {"fully patched", "mixed"}:
        dp.apply_dependency_patch(distribution=dist)
    if state == "mixed":
        for item in files[::2]:
            Path(dist.locate_file(item["path"])).write_bytes(originals[item["path"]])
    elif state == "unknown":
        Path(dist.locate_file(files[0]["path"])).write_text("unknown bytes\n")
    elif state == "wrong version":
        metadata = next(site.glob("*.dist-info/METADATA"))
        metadata.write_text(
            metadata.read_text().replace("Version: 0.5.22", "Version: 0.5.21")
        )
    return site


def test_manifest_is_closed_and_records_reviewed_upstream_artifacts():
    """Adding an unaudited manifest field or changing an approved digest breaks review."""
    manifest = _manifest()
    assert set(manifest) == {
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
    assert manifest["schema"] == 1
    assert manifest["distribution"] == "yamlgraph"
    assert manifest["version"] == "0.5.22"
    assert manifest["upstream_repository"] == "https://github.com/sheikkinen/yamlgraph"
    assert manifest["upstream_issue"] == "https://github.com/sheikkinen/yamlgraph/issues/474"
    assert manifest["upstream_patch_comment"] == (
        "https://github.com/sheikkinen/yamlgraph/issues/474#issuecomment-5354688575"
    )
    assert manifest["upstream_issue_body_sha256"] == (
        "c4ba42b9fd178dc56fad8d736e16b3dd58705112cfd2e31e5d9e3355c58dcc69"
    )
    assert manifest["upstream_patch_sha256"] == (
        "dbae9dfa437adcef6d3d60d6a7bb9be44336ba6ca92fd5f568b99412f9b3fec0"
    )
    assert manifest["patch_sha256"] == (
        "2af7f84c2663be4d8416e3a6e0f648b823a89d3b0ae15ee9daaf0c8e4d32d2d6"
    )
    assert manifest["files"] == sorted(manifest["files"], key=lambda item: item["path"])
    assert {item["path"] for item in manifest["files"]} == {
        "yamlgraph/compile/node_otel.py",
        "yamlgraph/compile/subgraph_relay.py",
        "yamlgraph/node_factory/subgraph_nodes.py",
        "yamlgraph/node_timeout.py",
    }


def test_exact_original_application_and_idempotent_rerun(tmp_path, monkeypatch):
    """Skipping before/after verification can silently misapply a context patch."""
    site, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "hostile-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "hostile-work-tree"))
    result = dp.apply_dependency_patch(distribution=dist)
    after = _states(dist)
    second = dp.apply_dependency_patch(distribution=dist)

    assert result.status == "patched"
    assert second.status == "already patched"
    assert before == {item["path"]: item["before_sha256"] for item in _manifest()["files"]}
    assert after == {item["path"]: item["after_sha256"] for item in _manifest()["files"]}
    assert (site / "outside.txt").read_text() == "untouched"


def test_default_installer_applies_and_verifies_native_join_patch(tmp_path):
    """Startup must reject a legacy-only installation missing the native barrier."""
    site, dist = _copy_official_distribution(tmp_path)
    manifest = json.loads((_canonical_paths()[0].parent / "native-join-manifest.json").read_text())
    legacy_manifest = tmp_path / "legacy-manifest.json"
    shutil.copyfile(_canonical_paths()[0], legacy_manifest)
    dp.apply_dependency_patch(distribution=dist, manifest_path=legacy_manifest)
    with pytest.raises(dp.DependencyPatchError, match="original"):
        dp.verify_dependency_patch(distribution=dist)
    dp.apply_dependency_patch(distribution=dist)
    assert all(
        _sha256(Path(dist.locate_file(item["path"]))) == item["after_sha256"]
        for item in manifest["files"]
    )
    assert dp.verify_dependency_patch(distribution=dist).status == "fully patched"
    assert dp.apply_dependency_patch(distribution=dist).status == "already patched"
    target = Path(dist.locate_file(manifest["files"][0]["path"]))
    target.write_text(target.read_text() + "\n# unreviewed barrier change\n")
    with pytest.raises(dp.DependencyPatchError):
        dp.verify_dependency_patch(distribution=dist)


def test_patch_digest_mismatch_fails_before_modification(tmp_path):
    """A replaced patch artifact must never be applied under an approved manifest."""
    _, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    manifest_path, patch_path = _canonical_paths()
    corrupt = tmp_path / "corrupt.patch"
    corrupt.write_bytes(patch_path.read_bytes() + b"\n# changed\n")

    with pytest.raises(dp.DependencyPatchError, match="patch digest mismatch"):
        dp.apply_dependency_patch(
            distribution=dist,
            manifest_path=manifest_path,
            patch_path=corrupt,
        )
    assert _states(dist) == before


@pytest.mark.parametrize("state", ["unknown", "mixed", "partial"])
def test_unknown_mixed_and_partial_states_fail_closed(tmp_path, state):
    """Anything other than all-before or all-after is not a safe patch state."""
    _, dist = _copy_official_distribution(tmp_path)
    files = _manifest()["files"]
    if state in {"mixed", "partial"}:
        originals = {
            item["path"]: Path(dist.locate_file(item["path"])).read_bytes()
            for item in files
        }
        dp.apply_dependency_patch(distribution=dist)
        targets = files[:1] if state == "partial" else files[::2]
        for item in targets:
            Path(dist.locate_file(item["path"])).write_bytes(originals[item["path"]])
    else:
        Path(dist.locate_file(files[0]["path"])).write_text("unknown bytes\n")

    with pytest.raises(dp.DependencyPatchError, match=state):
        dp.verify_dependency_patch(distribution=dist)


def test_wrong_version_refuses_even_when_files_match(tmp_path):
    """Matching source hashes do not authorize patching a different release."""
    site, _ = _copy_official_distribution(tmp_path)
    metadata = next(site.glob("*.dist-info/METADATA"))
    metadata.write_text(metadata.read_text().replace("Version: 0.5.22", "Version: 0.5.21"))
    dist = next(importlib.metadata.distributions(path=[str(site)]))

    with pytest.raises(dp.DependencyPatchError, match="wrong version"):
        dp.apply_dependency_patch(distribution=dist)


@pytest.mark.parametrize(
    ("probe_result", "message"),
    [(True, "patch obsolete"), (False, "patch diverged")],
)
def test_newer_versions_are_classified_by_native_probe(tmp_path, probe_result, message):
    """A new upstream release must never inherit or silently discard this patch."""
    site, _ = _copy_official_distribution(tmp_path)
    metadata = next(site.glob("*.dist-info/METADATA"))
    metadata.write_text(metadata.read_text().replace("Version: 0.5.22", "Version: 0.5.23"))
    dist = next(importlib.metadata.distributions(path=[str(site)]))

    with pytest.raises(dp.DependencyPatchError, match=message):
        dp.apply_dependency_patch(
            distribution=dist,
            capability_probe=lambda _python: probe_result,
        )


def test_replace_failure_restores_verified_originals_and_cleans_staging(tmp_path, monkeypatch):
    """A crash during replacement must not leave the installed package mixed."""
    _, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    real_replace = os.replace
    replaced = 0

    def fail_second_replacement(src, dst):
        nonlocal replaced
        if str(dst).endswith(".py"):
            replaced += 1
            if replaced == 2:
                raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(dp.os, "replace", fail_second_replacement)
    with pytest.raises(dp.DependencyPatchError, match="simulated replace failure"):
        dp.apply_dependency_patch(distribution=dist)
    assert _states(dist) == before


def test_post_replace_digest_failure_restores_verified_originals(tmp_path, monkeypatch):
    """A corrupted atomic output must roll back even after every replace returned."""
    _, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    real_atomic_write = dp._atomic_write_from
    writes = 0

    def corrupt_second_output(source, destination):
        nonlocal writes
        real_atomic_write(source, destination)
        writes += 1
        if writes == 2:
            destination.write_text("corrupted after replace\n")

    monkeypatch.setattr(dp, "_atomic_write_from", corrupt_second_output)
    with pytest.raises(dp.DependencyPatchError, match="patched output changed during replace"):
        dp.apply_dependency_patch(distribution=dist)
    assert _states(dist) == before


def test_final_state_read_failure_restores_verified_originals(tmp_path, monkeypatch):
    """Final classification must run while the original-byte backups still exist."""
    _, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    real_state = dp._state
    classifications = 0

    def fail_final_classification(distribution, manifest):
        nonlocal classifications
        classifications += 1
        if classifications == 2:
            raise OSError("simulated final state read failure")
        return real_state(distribution, manifest)

    monkeypatch.setattr(dp, "_state", fail_final_classification)
    with pytest.raises(dp.DependencyPatchError, match="final state read failure"):
        dp.apply_dependency_patch(distribution=dist)
    assert _states(dist) == before


def test_diff_paths_are_contained_and_exactly_match_manifest(tmp_path):
    """A valid digest cannot authorize traversal or an extra file in the diff."""
    _, dist = _copy_official_distribution(tmp_path)
    manifest_path, patch_path = _canonical_paths()
    unsafe = tmp_path / "unsafe.patch"
    unsafe.write_text(
        patch_path.read_text()
        + "diff --git a/../outside.txt b/../outside.txt\n"
        + "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-old\n+new\n"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["patch_sha256"] = hashlib.sha256(unsafe.read_bytes()).hexdigest()
    unsafe_manifest = tmp_path / "manifest.json"
    unsafe_manifest.write_text(json.dumps(manifest))

    with pytest.raises(dp.DependencyPatchError, match="unsafe dependency patch path"):
        dp.apply_dependency_patch(
            distribution=dist,
            manifest_path=unsafe_manifest,
            patch_path=unsafe,
        )


def test_read_only_verifier_never_modifies_original_or_mixed_state(tmp_path):
    """Ordinary application startup must remain a read-only state verifier."""
    _, dist = _copy_official_distribution(tmp_path)
    before = _states(dist)
    with pytest.raises(dp.DependencyPatchError, match="original"):
        dp.verify_dependency_patch(distribution=dist)
    assert _states(dist) == before


def test_module_bootstrap_fails_before_cli_import_on_original_distribution(tmp_path):
    """Importing the CLI before verification can import an unsafe yamlgraph runtime."""
    site, _ = _copy_official_distribution(tmp_path)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(ENGINE / "src")])}
    result = subprocess.run(
        [sys.executable, "-m", "lockstep", "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "dependency patch verification failed" in result.stderr.lower()


@pytest.mark.parametrize(
    "state",
    ["original", "fully patched", "mixed", "unknown", "wrong version"],
)
def test_console_and_module_bootstrap_fail_closed_state_matrix(tmp_path, state):
    """Both packaged startup surfaces must decide patch state before CLI import."""
    site = _distribution_in_state(tmp_path, state)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(ENGINE / "src")])}
    console = Path(sys.executable).with_name("lockstep")
    assert console.is_file()
    commands = ([str(console), "--help"], [sys.executable, "-m", "lockstep", "--help"])

    for command in commands:
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )
        if state == "fully patched":
            assert result.returncode == 0, result.stderr
            assert "usage:" in result.stdout.lower()
        else:
            assert result.returncode != 0
            assert "dependency patch verification failed" in result.stderr.lower()


def test_installer_cli_patches_then_module_bootstrap_starts(tmp_path):
    """A wheel/source installation needs one explicit applicator before startup."""
    site, _ = _copy_official_distribution(tmp_path)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(ENGINE / "src")])}
    install = subprocess.run(
        [sys.executable, "-m", "lockstep.dependency_patch"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    started = subprocess.run(
        [sys.executable, "-m", "lockstep", "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert install.returncode == 0, install.stderr
    assert "patched" in install.stdout
    assert started.returncode == 0, started.stderr
    assert "usage:" in started.stdout.lower()


def test_native_probe_rejects_official_original_and_accepts_patched_copy(tmp_path):
    """Version-change classification needs an executable behavior gate, not hashes."""
    site, dist = _copy_official_distribution(tmp_path)
    probe = ENGINE / "scripts" / "probe_yamlgraph_native.py"
    env = {**os.environ, "PYTHONPATH": str(site)}
    original = subprocess.run(
        [sys.executable, str(probe), "--quiet"], env=env, capture_output=True, text=True
    )
    dp.apply_dependency_patch(distribution=dist)
    patched = subprocess.run(
        [sys.executable, str(probe), "--quiet"], env=env, capture_output=True, text=True
    )
    assert original.returncode != 0
    assert patched.returncode == 0, patched.stderr


def test_packaged_probe_fallback_executes_adapter_owned_gate(monkeypatch):
    """A wheel without the source script must still delegate native imports to the adapter."""
    real_is_file = Path.is_file

    def hide_source_probe(path):
        if path.name == "probe_yamlgraph_native.py":
            return False
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", hide_source_probe)
    assert dp._default_capability_probe(sys.executable)


def test_black_box_uv_run_fails_closed_after_original_or_mixed_sync_state(tmp_path):
    """An implicit project run may never bypass a restored or mixed dependency."""
    project = tmp_path / "engine-copy"
    shutil.copytree(
        ENGINE,
        project,
        ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "dist"),
    )
    subprocess.run(
        ["uv", "sync", "--project", str(project), "--frozen"],
        check=True,
        capture_output=True,
        text=True,
    )

    def uv_run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--project", str(project), *args],
            capture_output=True,
            text=True,
        )

    original = uv_run("--no-sync", "lockstep", "--help")
    assert original.returncode != 0
    assert "dependency patch verification failed" in original.stderr.lower()

    installed = uv_run("--no-sync", "lockstep-dependency-install")
    assert installed.returncode == 0, installed.stderr
    assert uv_run("--no-sync", "lockstep", "--help").returncode == 0

    subprocess.run(
        [
            "uv",
            "sync",
            "--project",
            str(project),
            "--frozen",
            "--reinstall-package",
            "yamlgraph",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    restored = uv_run("lockstep", "--help")
    assert restored.returncode != 0
    assert "patch state is original" in restored.stderr.lower()

    package = next((project / ".venv" / "lib").glob("python*/site-packages/yamlgraph"))
    originals = {
        item["path"]: (package.parent / item["path"]).read_bytes()
        for item in _manifest()["files"]
    }
    assert uv_run("--no-sync", "lockstep-dependency-install").returncode == 0
    for item in _manifest()["files"][::2]:
        (package.parent / item["path"]).write_bytes(originals[item["path"]])

    mixed = uv_run("lockstep", "--help")
    assert mixed.returncode != 0
    assert "patch state is mixed" in mixed.stderr.lower()


def test_built_wheel_requires_explicit_packaged_installer(tmp_path):
    """A standalone wheel must fail closed until its own applicator runs."""
    dist_dir = tmp_path / "dist"
    venv = tmp_path / "clean-venv"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=ENGINE,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist_dir.glob("lockstep-*.whl"))
    subprocess.run(
        ["uv", "venv", str(venv)], check=True, capture_output=True, text=True
    )
    python = venv / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    lockstep = venv / "bin" / "lockstep"
    installer = venv / "bin" / "lockstep-dependency-install"

    before = subprocess.run([str(lockstep), "--help"], capture_output=True, text=True)
    first = subprocess.run([str(installer)], capture_output=True, text=True)
    second = subprocess.run([str(installer)], capture_output=True, text=True)
    after = subprocess.run([str(lockstep), "--help"], capture_output=True, text=True)
    module = subprocess.run(
        [str(python), "-m", "lockstep", "--help"], capture_output=True, text=True
    )

    assert before.returncode != 0
    assert "dependency patch verification failed" in before.stderr.lower()
    assert first.returncode == 0 and "patched" in first.stdout
    assert second.returncode == 0 and "already patched" in second.stdout
    assert after.returncode == 0 and "usage:" in after.stdout.lower()
    assert module.returncode == 0 and "usage:" in module.stdout.lower()
