"""Stable compiler-owned identities and state namespaces."""

from __future__ import annotations

import hashlib
import re


def _stable_id(pointer: str, kind: str, role: str) -> str:
    digest = hashlib.sha256(
        b"lockstep.workflow-node/v1\0"
        + pointer.encode("utf-8")
        + b"\0"
        + kind.encode("ascii")
        + b"\0"
        + role.encode("ascii")
    ).hexdigest()[:12]
    stem = pointer.rsplit("/", 1)[-1] or "root"
    return f"{kind}-{stem}-{role}-{digest}"


def _fragment_state_namespace(namespace: str) -> str:
    return hashlib.sha256(
        b"lockstep.fragment-state-namespace/v1\0" + namespace.encode("utf-8")
    ).hexdigest()


def _specialized_state_key(namespace: str, key: str) -> str:
    candidate = f"{namespace}_{key}"
    if len(candidate.encode("utf-8")) <= 128 and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", candidate
    ):
        return candidate
    digest = hashlib.sha256(
        b"lockstep.specialized-state-key/v1\0"
        + namespace.encode("ascii")
        + b"\0"
        + key.encode("utf-8")
    ).hexdigest()
    return f"child_{digest}"
