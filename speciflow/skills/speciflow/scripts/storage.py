"""Private, storage-only transport for the explicitly invoked SpeciFlow skill."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, BinaryIO, Callable, Iterator


_OPERATIONS = frozenset({"resolve", "preview", "init"})
_LOCATOR_FIELDS = frozenset({"version", "anchor", "project_key", "storage_base", "data_root"})


class StorageConflict(RuntimeError):
    """The requested local storage state is ambiguous or unsafe to change."""


@dataclass(frozen=True)
class Locator:
    version: int
    anchor: Path
    project_key: str
    storage_base: Path
    data_root: Path


@dataclass(frozen=True)
class LocatorWrite:
    path: Path
    locator: Locator


@dataclass(frozen=True)
class ResolveRequest:
    cwd: Path
    explicit_base: Path | None = None
    explicit_anchor: Path | None = None


@dataclass(frozen=True)
class Selection:
    request: ResolveRequest
    source: str
    anchor: Path
    working_root: Path
    project_key: str
    storage_base: Path
    storage_locator_path: Path
    anchor_locator_path: Path | None
    data_root: Path


@dataclass(frozen=True)
class InitPreview:
    selection: Selection
    share_from_anchor: bool
    locator_writes: tuple[LocatorWrite, ...]
    directories_to_create: tuple[Path, ...]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _canonical_path(value: Path | str, *, directory: bool = False) -> Path:
    if not isinstance(value, (str, Path)):
        raise StorageConflict("invalid path")
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise StorageConflict("invalid path") from exc
    if directory and not path.is_dir():
        raise StorageConflict("path is not a directory")
    return path


def _project_key(anchor: Path) -> str:
    return hashlib.sha256(os.fsencode(str(anchor))).hexdigest()


def _account_home() -> Path:
    """Return the validated home selected by the standard POSIX resolver."""

    try:
        candidate = Path.home()
        if not candidate.is_absolute():
            raise StorageConflict("unknown account home")
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise StorageConflict("unknown account home") from exc
    if not resolved.is_dir():
        raise StorageConflict("unknown account home")
    return resolved


_GIT_DISCOVERY_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)


def _git_environ() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name not in _GIT_DISCOVERY_ENV}


def _git_facts(cwd: Path) -> tuple[Path, Path] | None:
    """Return (canonical common directory, canonical worktree root) if verified."""

    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=cwd,
            env=_git_environ(), check=False, capture_output=True, text=True,
        )
        if common.returncode != 0 or not common.stdout.strip():
            return None
        raw_common = common.stdout.strip()
        common_path = _canonical_path(
            cwd / raw_common if not Path(raw_common).is_absolute() else raw_common,
            directory=True,
        )
        bare = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"], cwd=cwd,
            env=_git_environ(), check=False, capture_output=True, text=True,
        )
        if bare.returncode != 0:
            return None
        if bare.stdout.strip() == "true":
            return common_path, common_path
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            env=_git_environ(), check=False, capture_output=True, text=True,
        )
        if top.returncode != 0 or not top.stdout.strip():
            return None
        root = _canonical_path(top.stdout.strip(), directory=True)
        if cwd != root and root not in cwd.parents:
            return None
        return common_path, root
    except (OSError, StorageConflict):
        return None


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _locator_from_path(path: Path, *, anchor: Path, key: str, base: Path) -> Locator:
    if _is_symlink(path):
        raise StorageConflict("locator is a symlink")
    try:
        raw = path.read_text(encoding="utf-8")

        def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError("duplicate locator field")
                result[name] = value
            return result

        value = json.loads(raw, object_pairs_hook=object_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StorageConflict("invalid locator") from exc
    if not isinstance(value, dict) or frozenset(value) != _LOCATOR_FIELDS:
        raise StorageConflict("invalid locator")
    if type(value["version"]) is not int or value["version"] != 1:
        raise StorageConflict("invalid locator")
    if not all(isinstance(value[name], str) for name in _LOCATOR_FIELDS - {"version"}):
        raise StorageConflict("invalid locator")
    try:
        locator = Locator(
            1, _canonical_path(value["anchor"]), value["project_key"],
            _canonical_path(value["storage_base"]), _canonical_path(value["data_root"]),
        )
    except StorageConflict as exc:
        raise StorageConflict("invalid locator") from exc
    expected_data_root = base / "projects" / key
    if (locator.anchor != anchor or locator.project_key != key
            or locator.storage_base != base or locator.data_root != expected_data_root
            or locator.project_key != _project_key(locator.anchor)):
        raise StorageConflict("ambiguous locator")
    return locator


def _nearest_base(
    cwd: Path, *, anchor: Path | None, git: bool,
    default_base: Callable[[], Path] | None = None,
) -> tuple[Path, Path] | None:
    for parent in (cwd, *cwd.parents):
        base = parent / ".speciflow"
        if not _lexists(base):
            continue
        if _is_symlink(base) or not base.is_dir():
            raise StorageConflict("storage base is not a directory")
        base = base.resolve()
        if default_base is not None:
            try:
                if base == default_base():
                    continue
            except StorageConflict:
                pass
        if git:
            return base, anchor if anchor is not None else cwd
        if anchor is not None:
            return base, anchor
        if parent == cwd:
            return base, parent
        marker = base / "anchor-locator-v1.json"
        if not _lexists(marker):
            raise StorageConflict("ambiguous non-Git ancestor storage")
        marker_locator = _locator_from_path(
            marker, anchor=parent.resolve(), key=_project_key(parent.resolve()), base=base
        )
        if marker_locator.anchor == parent.resolve():
            return base, parent.resolve()
    return None


def _storage_locator(base: Path, key: str) -> Path:
    return base / "locators" / key / "locator-v1.json"


def resolve(request: ResolveRequest) -> Selection:
    """Select storage without changing the filesystem."""

    if not isinstance(request, ResolveRequest):
        raise TypeError("ResolveRequest is required")
    cwd = _canonical_path(request.cwd, directory=True)
    git = _git_facts(cwd)
    requested_anchor = None if request.explicit_anchor is None else _canonical_path(request.explicit_anchor, directory=True)
    if git is not None:
        anchor, working_root = git
    else:
        anchor = requested_anchor if requested_anchor is not None else cwd
        working_root = cwd
    explicit_base = None if request.explicit_base is None else _canonical_path(request.explicit_base)
    if explicit_base is not None:
        source, base = "explicit", explicit_base
    else:
        default_base: Path | None = None

        def account_default() -> Path:
            nonlocal default_base
            if default_base is None:
                default_base = _account_home() / ".speciflow"
            return default_base

        nearest = _nearest_base(
            cwd, anchor=requested_anchor if git is None else anchor, git=git is not None,
            default_base=account_default,
        )
        if nearest is not None:
            source, base = "ancestor", nearest[0]
            if git is None:
                anchor = nearest[1]
                working_root = anchor
        elif git is not None:
            common_locator_path = anchor / "speciflow" / "locator-v1.json"
            key = _project_key(anchor)
            if _lexists(common_locator_path):
                try:
                    common_raw = json.loads(common_locator_path.read_text(encoding="utf-8"))
                    common_base = _canonical_path(common_raw["storage_base"])
                except (OSError, json.JSONDecodeError, KeyError, TypeError, StorageConflict) as exc:
                    raise StorageConflict("ambiguous locator") from exc
                common_locator = _locator_from_path(common_locator_path, anchor=anchor, key=key, base=common_base)
                source, base = "git_common", common_locator.storage_base
            else:
                source, base = "default", account_default()
        else:
            source, base = "default", account_default()
    key = _project_key(anchor)
    storage_path = _storage_locator(base, key)
    if git is not None:
        anchor_path: Path | None = anchor / "speciflow" / "locator-v1.json"
    elif base == anchor / ".speciflow":
        anchor_path = base / "anchor-locator-v1.json"
    else:
        anchor_path = None
    data_root = base / "projects" / key
    stored: Locator | None = None
    if _lexists(storage_path):
        stored = _locator_from_path(storage_path, anchor=anchor, key=key, base=base)
        data_root = stored.data_root
    anchor_was_discovered = source == "git_common" or (
        git is None and source == "ancestor" and requested_anchor is None and cwd != anchor
    )
    if anchor_was_discovered and anchor_path is not None and _lexists(anchor_path):
        anchored = _locator_from_path(anchor_path, anchor=anchor, key=key, base=base)
        if stored is None:
            raise StorageConflict("partial locator state")
        if anchored != stored:
            raise StorageConflict("locator mismatch")
    return Selection(
        ResolveRequest(cwd, explicit_base, requested_anchor),
        source, anchor, working_root, key, base, storage_path, anchor_path, data_root,
    )


def _locator(selection: Selection) -> Locator:
    return Locator(1, selection.anchor, selection.project_key, selection.storage_base, selection.data_root)


def _write_needed(path: Path, locator: Locator) -> bool:
    if not _lexists(path):
        return True
    existing = _locator_from_path(path, anchor=locator.anchor, key=locator.project_key, base=locator.storage_base)
    if existing != locator:
        raise StorageConflict("locator mismatch")
    return False


def _missing_directories(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        missing: list[Path] = []
        current = path
        while not _lexists(current):
            missing.append(current)
            if current.parent == current:
                raise StorageConflict("directory collision")
            current = current.parent
        if _is_symlink(current) or not current.is_dir():
            raise StorageConflict("directory collision")
        for directory in reversed(missing):
            if directory not in result:
                result.append(directory)
    return tuple(result)


def preview_init(selection: Selection, share_from_anchor: bool = False) -> InitPreview:
    """Describe the exact helper-owned storage writes without performing them."""

    if not isinstance(selection, Selection) or not isinstance(share_from_anchor, bool):
        raise TypeError("Selection and bool are required")
    locator = _locator(selection)
    writes: list[LocatorWrite] = []
    if _write_needed(selection.storage_locator_path, locator):
        writes.append(LocatorWrite(selection.storage_locator_path, locator))
    include_anchor_locator = selection.anchor_locator_path is not None and (
        (selection.anchor_locator_path.name == "locator-v1.json" and share_from_anchor)
        or (selection.anchor_locator_path.name == "anchor-locator-v1.json" and selection.source == "explicit")
    )
    if include_anchor_locator:
        if _write_needed(selection.anchor_locator_path, locator):
            writes.append(LocatorWrite(selection.anchor_locator_path, locator))
    directories: list[Path] = [selection.storage_base, selection.storage_base / "locators", selection.storage_locator_path.parent]
    directories.extend((selection.storage_base / "projects", selection.data_root))
    if any(write.path == selection.anchor_locator_path for write in writes) and selection.anchor_locator_path is not None:
        directories.append(selection.anchor_locator_path.parent)
    return InitPreview(selection, share_from_anchor, tuple(writes), _missing_directories(tuple(dict.fromkeys(directories))))


def _locator_value(locator: Locator) -> dict[str, object]:
    return {"version": locator.version, "anchor": str(locator.anchor), "project_key": locator.project_key,
            "storage_base": str(locator.storage_base), "data_root": str(locator.data_root)}


def _request_value(request: ResolveRequest) -> dict[str, object]:
    return {"cwd": str(request.cwd), "explicit_base": None if request.explicit_base is None else str(request.explicit_base),
            "explicit_anchor": None if request.explicit_anchor is None else str(request.explicit_anchor)}


def _selection_value(selection: Selection) -> dict[str, object]:
    return {"request": _request_value(selection.request), "source": selection.source, "anchor": str(selection.anchor),
            "working_root": str(selection.working_root), "project_key": selection.project_key,
            "storage_base": str(selection.storage_base), "storage_locator_path": str(selection.storage_locator_path),
            "anchor_locator_path": None if selection.anchor_locator_path is None else str(selection.anchor_locator_path),
            "data_root": str(selection.data_root)}


def _preview_value(preview: InitPreview) -> dict[str, object]:
    return {"selection": _selection_value(preview.selection), "share_from_anchor": preview.share_from_anchor,
            "locator_writes": [{"path": str(write.path), "locator": _locator_value(write.locator)} for write in preview.locator_writes],
            "directories_to_create": [str(path) for path in preview.directories_to_create]}


def canonical_preview_bytes(preview: InitPreview) -> bytes:
    """Return compact sorted UTF-8 preview JSON; previews are never persisted."""

    if not isinstance(preview, InitPreview):
        raise TypeError("InitPreview is required")
    return _canonical_bytes(_preview_value(preview))


def _safe_mkdir(path: Path) -> None:
    if _lexists(path):
        if _is_symlink(path) or not path.is_dir():
            raise StorageConflict("directory collision")
        return
    if _is_symlink(path.parent) or not path.parent.is_dir():
        raise StorageConflict("unapproved parent directory")
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        if _is_symlink(path) or not path.is_dir():
            raise StorageConflict("directory collision")
    except OSError as exc:
        raise StorageConflict("unable to create directory") from exc


@contextmanager
def _directory_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except ImportError:
            pass
        os.close(fd)


def _create_locator(write: LocatorWrite) -> None:
    if _lexists(write.path):
        if not _write_needed(write.path, write.locator):
            return
        raise StorageConflict("locator collision")
    payload = _canonical_bytes(_locator_value(write.locator))
    try:
        fd = os.open(write.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _lexists(write.path) and not _write_needed(write.path, write.locator):
            return
        raise StorageConflict("locator collision")
    except OSError as exc:
        raise StorageConflict("unable to create locator") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(write.path, 0o600)
    except OSError as exc:
        raise StorageConflict("unable to write locator") from exc


def _validate_init_state(preview: InitPreview, *, data_root_created_here: bool = False) -> None:
    selection = preview.selection
    paths = [selection.storage_locator_path]
    if selection.anchor_locator_path is not None and (
        (selection.anchor_locator_path.name == "locator-v1.json" and preview.share_from_anchor)
        or (selection.anchor_locator_path.name == "anchor-locator-v1.json" and selection.source == "explicit")
    ):
        paths.append(selection.anchor_locator_path)
    present = tuple(_lexists(path) for path in paths)
    root_present = _lexists(selection.data_root)
    if root_present and (_is_symlink(selection.data_root) or not selection.data_root.is_dir()):
        raise StorageConflict("data root collision")
    if any(present):
        if not all(present) or not root_present:
            raise StorageConflict("partial initialization")
        _validate_owned_permissions(preview, paths)
        return
    if root_present and not data_root_created_here:
        raise StorageConflict("partial initialization")
    _validate_owned_permissions(preview, paths)


def _validate_owned_permissions(preview: InitPreview, locator_paths: list[Path]) -> None:
    selection = preview.selection
    directories = {
        selection.storage_base,
        selection.storage_base / "locators",
        selection.storage_locator_path.parent,
        selection.storage_base / "projects",
        selection.data_root,
    }
    directories.update(path.parent for path in locator_paths)
    for path in directories:
        if _lexists(path) and stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o700:
            raise StorageConflict("unsafe directory permissions")
    for path in locator_paths:
        if _lexists(path) and stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o600:
            raise StorageConflict("unsafe locator permissions")


def init(approved_preview: InitPreview) -> Locator:
    """Re-resolve and apply exactly the still-current approved storage preview."""

    if not isinstance(approved_preview, InitPreview):
        raise TypeError("InitPreview is required")
    current = preview_init(resolve(approved_preview.selection.request), approved_preview.share_from_anchor)
    if canonical_preview_bytes(current) != canonical_preview_bytes(approved_preview):
        raise StorageConflict("approved preview is no longer current")
    _validate_init_state(current)
    for directory in current.directories_to_create:
        _safe_mkdir(directory)
    lock_directories = tuple(sorted({write.path.parent for write in current.locator_writes}, key=str))
    with ExitStack() as stack:
        for directory in lock_directories:
            stack.enter_context(_directory_lock(directory))
        _validate_init_state(current, data_root_created_here=True)
        for write in current.locator_writes:
            _create_locator(write)
    return _locator(current.selection)


def _write(stdout: BinaryIO, result: dict[str, Any]) -> None:
    stdout.write(_canonical_bytes(result))


def _error(code: str) -> dict[str, str]:
    return {"error": code}


def _request(stdin: BinaryIO) -> dict[str, Any] | None:
    payload = stdin.read()
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _request_from_value(value: object) -> ResolveRequest:
    if not isinstance(value, dict):
        raise StorageConflict("invalid request")
    allowed = {"cwd", "explicit_base", "explicit_anchor", "op", "share_from_anchor", "approved_preview"}
    if set(value) - allowed:
        raise StorageConflict("invalid request")
    raw_cwd = value.get("cwd", str(Path.cwd()))
    if not isinstance(raw_cwd, str):
        raise StorageConflict("invalid path")
    arguments: dict[str, Path | None] = {"cwd": Path(raw_cwd)}
    for name in ("explicit_base", "explicit_anchor"):
        raw = value.get(name)
        if raw is not None and not isinstance(raw, str):
            raise StorageConflict("invalid path")
        arguments[name] = None if raw is None else Path(raw)
    return ResolveRequest(**arguments)  # type: ignore[arg-type]


def _preview_from_value(value: object) -> InitPreview:
    if not isinstance(value, dict) or not isinstance(value.get("selection"), dict):
        raise StorageConflict("invalid approved preview")
    selection_value = value["selection"]
    request = _request_from_value(selection_value.get("request"))
    share = value.get("share_from_anchor")
    if not isinstance(share, bool):
        raise StorageConflict("invalid approved preview")
    preview = preview_init(resolve(request), share)
    if canonical_preview_bytes(preview) != _canonical_bytes(value):
        raise StorageConflict("invalid approved preview")
    return preview


def main(stdin: BinaryIO, stdout: BinaryIO) -> int:
    value = _request(stdin)
    if value is None:
        _write(stdout, _error("invalid_request"))
        return 0
    operation = value.get("op")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        _write(stdout, _error("invalid_operation"))
        return 0
    try:
        if operation == "resolve":
            result: dict[str, Any] = _selection_value(resolve(_request_from_value(value)))
        elif operation == "preview":
            selection = resolve(_request_from_value(value))
            result = _preview_value(preview_init(selection, value.get("share_from_anchor", False)))
        else:
            preview = _preview_from_value(value.get("approved_preview"))
            result = {"initialized": True, "locator": _locator_value(init(preview))}
    except TypeError:
        result = _error("storage_conflict")
    except StorageConflict as exc:
        if str(exc) in {"invalid path", "path is not a directory"}:
            result = _error("invalid_path")
        elif operation == "init" and str(exc) in {"invalid approved preview", "approved preview is no longer current"}:
            result = _error("preview_mismatch")
        else:
            result = _error("storage_conflict")
    _write(stdout, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.buffer, sys.stdout.buffer))
