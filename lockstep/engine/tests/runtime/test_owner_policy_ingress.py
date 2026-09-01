"""Owner runtime provisioning ingress is one closed parsed boundary."""

from __future__ import annotations

import json

import pytest


def _config() -> dict[str, object]:
    common: dict[str, object] = {
        "executable": "/bin/codex",
        "model": "model",
        "cli_version": "version",
        "permission_profile": {
            "sandbox": "workspace-write",
            "approval": "never",
        },
        "codex_home": "/owner/codex",
        "environment": {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": "/owner/tmp",
        },
    }
    return {
        "schema": "lockstep.runtime-provision-config/v1",
        "codex": dict(common),
        "pinned": {
            **common,
            "codex_home": "/owner/pinned",
            "pinned_permission_profile": "owner-profile",
        },
    }


def _documents(
    *,
    config: object | None = None,
    grants: object | None = None,
) -> tuple[bytes, bytes]:
    return (
        json.dumps(_config() if config is None else config).encode(),
        json.dumps([] if grants is None else grants).encode(),
    )


def test_runtime_provision_documents_normalize_replacement_keys() -> None:
    from lockstep.runtime.effects.owner_policy_ingress import (
        parse_runtime_provision_documents,
    )

    high = "f" * 64
    low = "0" * 64
    _codex, _pinned, keys = parse_runtime_provision_documents(
        *_documents(grants=[high, low])
    )

    assert keys == (low, high)


@pytest.mark.parametrize("member", ["schema", "model"])
def test_runtime_provision_config_rejects_duplicate_object_members(
    member: str,
) -> None:
    from lockstep.runtime.effects.owner_policy_ingress import (
        parse_runtime_provision_documents,
    )

    encoded = json.dumps(_config(), separators=(",", ":"))
    if member == "schema":
        encoded = encoded.replace(
            '"schema":"lockstep.runtime-provision-config/v1"',
            '"schema":"lockstep.runtime-provision-config/v1",'
            '"schema":"lockstep.runtime-provision-config/v1"',
            1,
        )
    else:
        encoded = encoded.replace(
            '"model":"model"',
            '"model":"reviewed","model":"effective"',
            1,
        )

    with pytest.raises(ValueError, match="duplicate object member"):
        parse_runtime_provision_documents(encoded.encode(), b"[]")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("config-utf8", "config must be UTF-8"),
        ("config-json", "config must be valid JSON"),
        ("config-object", "config must be a JSON object"),
        ("config-schema", "exact config schema"),
        ("binding-object", "codex must be a JSON object"),
        ("binding-schema", "exact pinned binding schema"),
        ("permission-profile-type", "permission profile"),
        ("permission-profile-value", "permission profile"),
        ("environment-type", "environment"),
        ("string-field-type", "non-empty strings"),
        ("same-homes", "homes must differ"),
        ("pinned-profile-empty", "pinned permission profile"),
        ("grants-utf8", "replacement grants must be UTF-8"),
        ("grants-json", "replacement grants must be valid JSON"),
        ("grants-object", "replacement grants must be a JSON array"),
        ("grants-uppercase", "lowercase SHA-256"),
        ("grants-duplicate", "unique"),
        ("grants-overflow", "at most 4096"),
    ],
)
def test_runtime_provision_documents_reject_invalid_domain(
    mutation: str,
    expected: str,
) -> None:
    from lockstep.runtime.effects.owner_policy_ingress import (
        parse_runtime_provision_documents,
    )

    config = _config()
    grants: object = []
    config_bytes: bytes | None = None
    grants_bytes: bytes | None = None
    if mutation == "config-utf8":
        config_bytes = b"\xff"
    elif mutation == "config-json":
        config_bytes = b"{"
    elif mutation == "config-object":
        config = []
    elif mutation == "config-schema":
        config["extra"] = None
    elif mutation == "binding-object":
        config["codex"] = []
    elif mutation == "binding-schema":
        pinned = config["pinned"]
        assert isinstance(pinned, dict)
        pinned["extra"] = None
    elif mutation == "permission-profile-type":
        codex = config["codex"]
        assert isinstance(codex, dict)
        codex["permission_profile"] = ["sandbox", "approval"]
    elif mutation == "permission-profile-value":
        codex = config["codex"]
        assert isinstance(codex, dict)
        codex["permission_profile"] = {
            "sandbox": "workspace-write",
            "approval": "on-request",
        }
    elif mutation == "environment-type":
        pinned = config["pinned"]
        assert isinstance(pinned, dict)
        pinned["environment"] = []
    elif mutation == "string-field-type":
        codex = config["codex"]
        assert isinstance(codex, dict)
        codex["model"] = 1
    elif mutation == "same-homes":
        pinned = config["pinned"]
        assert isinstance(pinned, dict)
        pinned["codex_home"] = "/owner/codex"
    elif mutation == "pinned-profile-empty":
        pinned = config["pinned"]
        assert isinstance(pinned, dict)
        pinned["pinned_permission_profile"] = ""
    elif mutation == "grants-utf8":
        grants_bytes = b"\xff"
    elif mutation == "grants-json":
        grants_bytes = b"["
    elif mutation == "grants-object":
        grants = {}
    elif mutation == "grants-uppercase":
        grants = ["A" * 64]
    elif mutation == "grants-duplicate":
        grants = ["0" * 64, "0" * 64]
    elif mutation == "grants-overflow":
        grants = [f"{index:064x}" for index in range(4097)]
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    encoded_config, encoded_grants = _documents(config=config, grants=grants)
    with pytest.raises(ValueError, match=expected):
        parse_runtime_provision_documents(
            config_bytes if config_bytes is not None else encoded_config,
            grants_bytes if grants_bytes is not None else encoded_grants,
        )
