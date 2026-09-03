"""Shared stateless mechanics for CLI boundary adapters."""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

from lockstep.errors import AuthoringError


def current_project() -> Path:
    return Path.cwd().resolve()


def decode_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthoringError(f"{label} must be JSON") from exc
    if not isinstance(value, dict):
        raise AuthoringError(f"{label} must be a JSON object")
    return value


def write_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


def require_owner_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AuthoringError("owner consent issuance and revocation require a TTY")


def read_consent_token() -> str:
    if sys.stdin.isatty():
        token = getpass.getpass("Publication consent token: ")
    else:
        token = sys.stdin.readline(4098).rstrip("\r\n")
    if not token:
        raise AuthoringError("publication consent token is required")
    if len(token.encode("utf-8")) > 4096:
        raise AuthoringError("publication consent token is too long")
    return token
