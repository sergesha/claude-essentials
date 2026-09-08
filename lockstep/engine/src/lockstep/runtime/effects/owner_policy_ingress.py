"""Closed parsing boundary for owner runtime provisioning documents."""

from __future__ import annotations

import json
from pathlib import Path
import re
from dataclasses import dataclass
from lockstep.runtime.providers.local import PinnedBackend, local_environment


class _DuplicateJsonMember(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise _DuplicateJsonMember(key)
        value[key] = member
    return value


def _json_document(data: bytes, *, label: str) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"runtime {label} must be UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime {label} must be valid JSON") from exc
    except _DuplicateJsonMember as exc:
        raise ValueError(
            f"runtime {label} contains duplicate object member {exc.args[0]!r}"
        ) from exc


def _provision_binding(
    value: object,
    *,
    label: str,
    pinned: bool,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"runtime provision {label} must be a JSON object")
    if pinned and "backend" in value:
        if (value["backend"] != PinnedBackend.DIRECT_LOCAL
                or set(value) != {"backend", "environment"}):
            raise ValueError("runtime provision direct-local binding schema is invalid")
        local_environment(value["environment"])
        return value
    fields = {
        "executable",
        "model",
        "cli_version",
        "permission_profile",
        "codex_home",
        "environment",
    }
    if pinned:
        fields.add("pinned_permission_profile")
    if set(value) != fields:
        raise ValueError(
            f"runtime provision config must use the exact {label} binding schema"
        )
    string_fields = ("executable", "model", "cli_version", "codex_home")
    if any(
        not isinstance(value[field], str) or not value[field] or "\x00" in value[field]
        for field in string_fields
    ):
        raise ValueError(
            f"runtime provision {label} paths and identities must be non-empty strings"
        )
    if (
        not Path(value["executable"]).is_absolute()
        or not Path(value["codex_home"]).is_absolute()
    ):
        raise ValueError(f"runtime provision {label} paths must be absolute")
    profile = value["permission_profile"]
    if not isinstance(profile, dict) or profile != {
        "sandbox": "workspace-write",
        "approval": "never",
    }:
        raise ValueError(
            "runtime provision permission profile must exactly require "
            "workspace-write and never approval"
        )
    environment = value["environment"]
    if not isinstance(environment, dict) or set(environment) != {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    }:
        raise ValueError(
            "runtime provision environment must define exactly PATH, LANG, LC_ALL, and TMPDIR"
        )
    if any(
        not isinstance(item, str) or not item or "\x00" in item
        for item in environment.values()
    ):
        raise ValueError("runtime provision environment contains an invalid value")
    if not Path(environment["TMPDIR"]).is_absolute():
        raise ValueError("runtime provision TMPDIR must be absolute")
    if pinned:
        pinned_profile = value["pinned_permission_profile"]
        if (
            not isinstance(pinned_profile, str)
            or not pinned_profile
            or "\x00" in pinned_profile
            or len(pinned_profile.encode("utf-8")) > 4096
        ):
            raise ValueError("pinned permission profile must be owner-selected")
    return value


@dataclass(frozen=True)
class RuntimeProvisionConfig:
    codex: dict[str, object] | None
    pinned: dict[str, object] | None
    claude: dict[str, object] | None
    replacement_keys: tuple[str, ...]


def _claude_provision_binding(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"executable", "model", "home", "environment"}:
        raise ValueError("runtime provision Claude binding schema is invalid")
    if any(not isinstance(value[key], str) or not value[key] or "\x00" in value[key]
           for key in ("executable", "model", "home")):
        raise ValueError("runtime provision Claude identities must be non-empty strings")
    if not Path(value["executable"]).is_absolute() or not Path(value["home"]).is_absolute():
        raise ValueError("runtime provision Claude paths must be absolute")
    local_environment(value["environment"])
    return value


def parse_runtime_provision_documents(
    config_bytes: bytes,
    replacement_bytes: bytes,
) -> RuntimeProvisionConfig:
    """Parse both untrusted provisioning documents into one closed domain."""

    config = _json_document(config_bytes, label="provision config")
    if not isinstance(config, dict):
        raise ValueError("runtime provision config must be a JSON object")
    if (
        not set(config) <= {"schema", "codex", "pinned", "claude"}
        or config.get("schema") != "lockstep.runtime-provision-config/v1"
    ):
        raise ValueError("runtime provision config must use the exact config schema")
    codex = (
        _provision_binding(config["codex"], label="codex", pinned=False)
        if "codex" in config
        else None
    )
    pinned = (
        _provision_binding(config["pinned"], label="pinned", pinned=True)
        if "pinned" in config
        else None
    )
    claude = _claude_provision_binding(config["claude"]) if "claude" in config else None
    if (
        codex is not None
        and pinned is not None
        and "codex_home" in pinned
        and codex["codex_home"] == pinned["codex_home"]
    ):
        raise ValueError("runtime provision Codex homes must differ")

    replacement = _json_document(replacement_bytes, label="replacement grants")
    if not isinstance(replacement, list):
        raise ValueError("runtime replacement grants must be a JSON array")
    if len(replacement) > 4096:
        raise ValueError("runtime replacement grants must contain at most 4096 keys")
    if any(
        not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None
        for key in replacement
    ):
        raise ValueError(
            "runtime replacement grants must contain lowercase SHA-256 keys"
        )
    if len(set(replacement)) != len(replacement):
        raise ValueError("runtime replacement grant keys must be unique")
    return RuntimeProvisionConfig(codex, pinned, claude, tuple(sorted(replacement)))
