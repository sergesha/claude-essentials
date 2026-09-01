from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from lockstep.runtime.manifests import (
    PathContractError,
    ProjectWritePath,
    capture_project,
    compare_effect,
)
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.project_paths import portable_collision_key, validate_portable_project_paths
from lockstep.runtime.providers._workspace_core import (
    WorkspaceContext,
    WorkspaceError,
    WorkspaceLease,
    _stat_identity,
)

class WorkspaceAttestor:
    def __init__(self, context: WorkspaceContext) -> None:
        self._context = context

    def _capture(self, workspace: Path) -> FilesystemSnapshot:
        try:
            return capture_project(workspace)
        except (OSError, PathContractError) as exc:
            raise WorkspaceError(
                f"workspace manifest integrity failure: {exc}"
            ) from exc

    def _preflight_tree_limits(self, workspace: Path) -> None:
        """Bound a quiescent tree before hashing or allocating file contents."""

        pending = [workspace]
        entries = 0
        total_bytes = 0
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        if child.name == ".git" and directory == workspace:
                            continue
                        entries += 1
                        if entries > self._context.limits.max_entries:
                            raise WorkspaceError(
                                "workspace entries exceed rollover admission limit"
                            )
                        metadata = child.stat(follow_symlinks=False)
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(Path(child.path))
                        elif stat.S_ISREG(metadata.st_mode):
                            if metadata.st_size > self._context.limits.max_file_bytes:
                                raise WorkspaceError(
                                    "workspace file exceeds rollover admission limit"
                                )
                            total_bytes += metadata.st_size
                            if total_bytes > self._context.limits.max_total_bytes:
                                raise WorkspaceError(
                                    "workspace bytes exceed rollover admission limit"
                                )
            except OSError as exc:
                raise WorkspaceError(
                    "workspace changed during rollover limit preflight"
                ) from exc

    def _vcs_tree_digest(self, workspace: Path) -> str:
        """Hash the complete local Git control tree without following links."""

        root = workspace / ".git"
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise WorkspaceError("Git control directory is missing") from exc
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(
            root_metadata.st_mode
        ):
            raise WorkspaceError("Git control directory is not a real directory")

        digest = hashlib.sha256(b"lockstep.git-control-tree/v1\0")
        entries = 0
        total_bytes = 0

        def walk(directory: Path, relative: str, expected: os.stat_result) -> None:
            nonlocal entries, total_bytes
            try:
                names: list[str] = []
                with os.scandir(directory) as children:
                    for child in children:
                        entries += 1
                        if entries > self._context.limits.max_entries:
                            raise WorkspaceError(
                                "Git control entries exceed admission limit"
                            )
                        names.append(child.name)
                seen: dict[str, str] = {}
                for name in sorted(names):
                    key = portable_collision_key(name)
                    previous = seen.get(key)
                    if previous is not None and previous != name:
                        raise WorkspaceError(
                            f"Git control path collision: {previous!r} and {name!r}"
                        )
                    seen[key] = name
                    path = directory / name
                    item = path.lstat()
                    rel = f"{relative}/{name}" if relative else name
                    encoded = rel.encode("utf-8", "surrogateescape")
                    if stat.S_ISLNK(item.st_mode):
                        raise WorkspaceError(f"symlink in Git control state: {rel}")
                    if stat.S_ISDIR(item.st_mode):
                        digest.update(b"directory\0" + encoded + b"\0")
                        digest.update(str(stat.S_IMODE(item.st_mode)).encode() + b"\0")
                        walk(path, rel, item)
                    elif stat.S_ISREG(item.st_mode):
                        if item.st_size > self._context.limits.max_file_bytes:
                            raise WorkspaceError(
                                "Git control file exceeds admission limit"
                            )
                        flags = (
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                        )
                        descriptor = os.open(path, flags)
                        try:
                            opened = os.fstat(descriptor)
                            if _stat_identity(opened) != _stat_identity(item):
                                raise WorkspaceError(
                                    f"Git control file changed while hashing: {rel}"
                                )
                            file_digest = hashlib.sha256()
                            observed_size = 0
                            while chunk := os.read(descriptor, 1024 * 1024):
                                observed_size += len(chunk)
                                total_bytes += len(chunk)
                                if observed_size > self._context.limits.max_file_bytes:
                                    raise WorkspaceError(
                                        "Git control file exceeds admission limit"
                                    )
                                if total_bytes > self._context.limits.max_total_bytes:
                                    raise WorkspaceError(
                                        "Git control bytes exceed admission limit"
                                    )
                                file_digest.update(chunk)
                            if _stat_identity(os.fstat(descriptor)) != _stat_identity(
                                item
                            ):
                                raise WorkspaceError(
                                    f"Git control file changed while hashing: {rel}"
                                )
                        finally:
                            os.close(descriptor)
                        digest.update(b"file\0" + encoded + b"\0")
                        digest.update(str(stat.S_IMODE(item.st_mode)).encode() + b"\0")
                        digest.update(file_digest.digest())
                    else:
                        raise WorkspaceError(
                            f"special file in Git control state: {rel}"
                        )
                if _stat_identity(directory.lstat()) != _stat_identity(expected):
                    raise WorkspaceError("Git control directory changed while hashing")
            except OSError as exc:
                raise WorkspaceError("Git control state changed while hashing") from exc

        walk(root, "", root_metadata)
        return digest.hexdigest()

    def _verify_exact_input(self, baseline: FilesystemSnapshot, snapshot) -> None:
        entries = tuple(entry for entry in baseline.entries if entry.kind == "file")
        if any(entry.kind == "symlink" for entry in baseline.entries):
            raise WorkspaceError("input workspace manifest contains a symlink")
        expected = tuple((entry.path, entry.blob.sha256) for entry in snapshot.files)
        observed = tuple((entry.path, entry.sha256) for entry in entries)
        if observed != expected:
            raise WorkspaceError(
                "materialized workspace does not match the exact input snapshot"
            )
        if baseline.git is None:
            raise WorkspaceError("materialized workspace lacks Git attestation")

    def _validate_output(
        self, lease: WorkspaceLease, captured: FilesystemSnapshot
    ) -> None:
        if self._vcs_tree_digest(lease.workspace_path) != lease.vcs_baseline_digest:
            raise WorkspaceError("Git control state changed")
        validate_portable_project_paths(
            (
                (entry.path, "directory" if entry.kind == "directory" else "file")
                for entry in captured.entries
            ),
            limits=self._context.limits,
            label="workspace entries",
        )
        if any(entry.kind == "symlink" for entry in captured.entries):
            raise WorkspaceError("workspace manifest integrity rejects symlink output")
        try:
            allowed = tuple(
                ProjectWritePath.parse(path, lease.workspace_path)
                for path in lease.declared_writes
            )
            comparison = compare_effect(lease.baseline, captured, allowed, "pass")
        except PathContractError as exc:
            raise WorkspaceError(
                f"workspace manifest integrity failure: {exc}"
            ) from exc
        if comparison.integrity_error:
            raise WorkspaceError("; ".join(comparison.reasons))

    @staticmethod
    def _validate_snapshot_fidelity(captured: FilesystemSnapshot) -> None:
        files = tuple(entry.path for entry in captured.entries if entry.kind == "file")
        executable = next(
            (
                entry.path
                for entry in captured.entries
                if entry.kind == "file" and entry.executable
            ),
            None,
        )
        if executable is not None:
            raise WorkspaceError(
                f"snapshot fidelity rejects executable output: {executable}"
            )
        for entry in captured.entries:
            if entry.kind == "directory" and not any(
                path.startswith(entry.path + "/") for path in files
            ):
                raise WorkspaceError(
                    f"snapshot fidelity rejects empty directory: {entry.path}"
                )

    @staticmethod
    def _relocated_baseline(
        baseline: FilesystemSnapshot, relocated: FilesystemSnapshot
    ) -> FilesystemSnapshot:
        """Rebind the path-sensitive Git marker after an atomic quarantine move."""

        old_git = baseline.git
        new_git = relocated.git
        if old_git is None or new_git is None:
            raise WorkspaceError("quarantined workspace lost its Git attestation")
        old_control = (
            old_git.head_sha256,
            old_git.index_sha256,
            old_git.worktree_config_sha256,
            old_git.worktree_config_worktree_sha256,
            old_git.common_config_sha256,
            old_git.common_refs_sha256,
        )
        new_control = (
            new_git.head_sha256,
            new_git.index_sha256,
            new_git.worktree_config_sha256,
            new_git.worktree_config_worktree_sha256,
            new_git.common_config_sha256,
            new_git.common_refs_sha256,
        )
        if old_control != new_control:
            raise WorkspaceError("Git control state changed before durable quarantine")
        return FilesystemSnapshot(entries=baseline.entries, git=new_git)

    def _initialize_git_control(self, workspace: Path) -> None:
        git = workspace / ".git"
        directories = (
            git,
            git / "hooks",
            git / "objects",
            git / "objects/info",
            git / "objects/pack",
            git / "refs",
            git / "refs/heads",
            git / "refs/tags",
        )
        for directory in directories:
            directory.mkdir(mode=0o700)
        files = {
            git / "HEAD": b"ref: refs/heads/lockstep\n",
            git / "config": (
                b"[core]\n"
                b"\trepositoryformatversion = 0\n"
                b"\tfilemode = true\n"
                b"\tbare = false\n"
                b"\tlogallrefupdates = true\n"
            ),
        }
        for path, data in files.items():
            self._write_private_file(path, data)

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _fsync_materialized_tree(self, workspace: Path) -> None:
        directories: list[Path] = []
        for current, _children, _files in os.walk(workspace, followlinks=False):
            directories.append(Path(current))
        for directory in reversed(directories):
            self._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
