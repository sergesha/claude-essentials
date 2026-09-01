"""Shared scalar and shape validation for the Workflow DSL schema parser."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from .diagnostics import Diagnostic, DiagnosticError
from .ir import SourceLocation

_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_V2_KEYS = frozenset(
    {
        "goto",
        "race",
        "cancel",
        "cancel_on_failure",
        "fail_fast",
        "speculative",
        "cleanup_deadline",
        "quorum",
        "weighted_quorum",
        "first_success",
        "first_terminal",
        "dynamic_branches",
        "branch_map",
        "map",
        "cross_machine",
        "patch",
        "patch_export",
        "merge",
        "merge_order",
        "conflict_resolution",
        "checkpoint",
        "resume",
        "migrate",
        "remote_heartbeat",
        "remote_lease",
        "artifact_store",
        "template",
        "templates",
        "template_registry",
        "plugin",
        "plugins",
        "runtime_compilation",
    }
)

SourceMark = SourceLocation


@dataclass(frozen=True)
class MarkedDocument:
    path: Path
    data: Any
    marks: Mapping[str, SourceMark]
    source_sha256: str

    def mark_for(self, pointer: str) -> SourceMark | None:
        current = pointer
        while current not in self.marks and current:
            current = current.rsplit("/", 1)[0]
        return self.marks.get(current) or self.marks.get("")


def _escape(pointer_part: str) -> str:
    return pointer_part.replace("~", "~0").replace("/", "~1")


class _SchemaValidation:
    def __init__(self, document: MarkedDocument) -> None:
        self.document = document

    def fail(self, code: str, message: str, pointer: str, hint: str) -> NoReturn:
        mark = self.document.mark_for(pointer)
        raise DiagnosticError(
            (
                Diagnostic(
                    code,
                    message,
                    self.document.path,
                    mark.line if mark else None,
                    mark.column if mark else None,
                    pointer,
                    hint,
                ),
            )
        )

    def mapping(self, value: Any, pointer: str, noun: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail(
                "LSW108",
                f"{noun} must be a mapping",
                pointer,
                f"make {noun} a YAML mapping",
            )
        return value

    def sequence(self, value: Any, pointer: str, noun: str) -> list[Any]:
        if not isinstance(value, list):
            self.fail(
                "LSW108",
                f"{noun} must be a list",
                pointer,
                f"make {noun} a YAML list",
            )
        return value

    def string(self, value: Any, pointer: str, noun: str) -> str:
        if not isinstance(value, str) or not value:
            self.fail(
                "LSW108",
                f"{noun} must be a non-empty string",
                pointer,
                f"provide a non-empty {noun}",
            )
        return value

    def positive_int(self, value: Any, pointer: str, noun: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self.fail(
                "LSW108",
                f"{noun} must be a positive integer",
                pointer,
                f"provide a positive {noun}",
            )
        return value

    def keys(
        self,
        value: dict[str, Any],
        pointer: str,
        allowed: Iterable[str],
        required: Iterable[str] = (),
    ) -> None:
        allowed_set = set(allowed)
        for key in value:
            key_pointer = f"{pointer}/{_escape(str(key))}"
            if not isinstance(key, str):
                self.fail(
                    "LSW105",
                    "mapping keys must be strings",
                    key_pointer,
                    "use a string key",
                )
            if key.startswith("x-"):
                continue
            if key in _V2_KEYS:
                self.fail(
                    "LSW120",
                    f"{key!r} is not available in Workflow DSL v1",
                    key_pointer,
                    "remove the v2-only key",
                )
            if key not in allowed_set:
                self.fail(
                    "LSW105",
                    f"unknown key {key!r}",
                    key_pointer,
                    "remove it or prefix inert metadata with x-",
                )
        for key in required:
            if key not in value:
                self.fail(
                    "LSW106",
                    f"missing required key {key!r}",
                    pointer,
                    f"add required key {key!r}",
                )

    def identifier(
        self,
        value: Any,
        pointer: str,
        noun: str = "id",
        optional: bool = False,
    ) -> str | None:
        if value is None and optional:
            return None
        text = self.string(value, pointer, noun)
        if not _ID.fullmatch(text):
            self.fail(
                "LSW110",
                f"invalid {noun} {text!r}",
                pointer,
                "use lowercase letters, digits, and hyphens, beginning with a letter",
            )
        return text

    def strings(self, value: Any, pointer: str, noun: str) -> tuple[str, ...]:
        items = self.sequence(value, pointer, noun)
        return tuple(
            self.string(item, f"{pointer}/{index}", noun.removesuffix("s"))
            for index, item in enumerate(items)
        )

    def handler(self, value: Any, pointer: str) -> str | None:
        if value is None:
            return None
        text = self.string(value, pointer, "outcome handler")
        if text != "escalate":
            self.fail(
                "LSW108",
                "v1 outcome handlers must be escalate",
                pointer,
                "use escalate",
            )
        return text
