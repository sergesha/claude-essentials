"""Value objects shared by workflow-lowering responsibilities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping  # noqa: UP035 - preserves public hints

from ._lowering_conditions import _rewrite_condition_references
from ._lowering_identity import _fragment_state_namespace


@dataclass(frozen=True)
class LoweredGeneratedFile:
    relative_path: str
    content: bytes
    sha256: str
    logical_name: str
    use_pointer: str
    definition_sha256: str


@dataclass(frozen=True)
class LoweredDependency:
    kind: str
    logical_name: str
    use_pointer: str
    definition_sha256: str
    compiled_sha256: str
    generated_root: str | None


@dataclass
class _Exit:
    source: str
    condition: str | None = None


@dataclass
class _Fragment:
    entry: str
    exits: list[_Exit]


@dataclass(frozen=True)
class _GraphFragmentPlan:
    raw: dict[str, Any]
    fragment: dict[str, Any]
    nodes: dict[str, Any]
    edges: list[Any]
    state: dict[str, Any]
    namespace: str
    local_names: frozenset[str]
    entry: str
    exits: dict[str, str]
    logical_name: str
    source_definition_sha256: str


@dataclass(frozen=True)
class _FragmentNames:
    namespace: str
    state: Mapping[str, Any]

    def node(self, name: str) -> str:
        return f"{self.namespace}.{name}"

    def state_key(self, name: str) -> str:
        return f"fragment_{_fragment_state_namespace(self.namespace)}_{name}"

    def identity(self, kind: str, name: str) -> str:
        digest = hashlib.sha256(
            b"lockstep.fragment-identity/v1\0"
            + kind.encode("ascii")
            + b"\0"
            + self.namespace.encode("utf-8")
            + b"\0"
            + name.encode("utf-8")
        ).hexdigest()
        return f"fragment-{kind}-{digest}"

    def template(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.template(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.template(item) for item in value]
        if not isinstance(value, str):
            return value
        referenced = re.findall(r"\{state\.([A-Za-z_][A-Za-z0-9_]*)", value)
        unknown = set(referenced) - set(self.state)
        if unknown:
            raise ValueError(
                f"fragment template references unknown state: {sorted(unknown)}"
            )
        rewritten = value
        for local_key in self.state:
            rewritten = rewritten.replace(
                f"{{state.{local_key}", f"{{state.{self.state_key(local_key)}"
            )
        return rewritten

    def condition(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _rewrite_condition_references(
            value,
            {local_key: self.state_key(local_key) for local_key in self.state},
            reject_unknown=True,
        )
