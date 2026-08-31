"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping  # noqa: UP035 - preserves existing hints

from ._lowering_contracts import _GraphFragmentPlan
from ._lowering_identity import _stable_id
from .canonical import canonical_yaml, plain
from .ir import FragmentIR, GraphIR


class _LoweringGraphPlan:
    def _graph_source(
        self,
        block: GraphIR,
        pointer: str,
    ) -> tuple[dict[str, Any], str, str]:
        if block.kind == "inline":
            inline_document = plain(block.graph or {})
            inline_document.pop("id", None)
            raw = plain(FragmentIR.parse(inline_document).document)
            source_definition_sha256 = hashlib.sha256(canonical_yaml(raw)).hexdigest()
            return raw, source_definition_sha256, f"inline:{pointer}"
        if self.catalog is None:
            raise ValueError("include_graph lowering requires a resolved catalog")
        resolver = getattr(self.catalog, "fragment_for", None)
        resolved = resolver(block.path) if callable(resolver) else None
        if resolved is None:
            raise ValueError(f"resolved fragment is unavailable for {block.path!r}")
        return (
            plain(resolved.fragment.document),
            resolved.source_definition_sha256,
            resolved.logical_path,
        )

    def _graph_plan(self, block: GraphIR, pointer: str) -> _GraphFragmentPlan:
        raw, source_definition_sha256, logical_name = self._graph_source(block, pointer)
        fragment, nodes, edges, state = self._closed_graph_components(raw)
        namespace = block.id or _stable_id(pointer, "graph", "namespace")
        local_names, entry, exits = self._closed_graph_boundary(fragment, nodes)
        return _GraphFragmentPlan(
            raw=raw,
            fragment=fragment,
            nodes=nodes,
            edges=edges,
            state=state,
            namespace=namespace,
            local_names=local_names,
            entry=entry,
            exits=exits,
            logical_name=logical_name,
            source_definition_sha256=source_definition_sha256,
        )

    @staticmethod
    def _closed_graph_components(
        raw: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[Any], dict[str, Any]]:
        fragment = raw.get("fragment")
        nodes = raw.get("nodes")
        edges = raw.get("edges")
        state = raw.get("state", {})
        if not isinstance(fragment, dict) or not isinstance(nodes, dict):
            raise ValueError("invalid closed graph fragment")  # noqa: TRY004
        if not isinstance(edges, list) or not isinstance(state, dict):
            raise ValueError("invalid closed graph fragment")  # noqa: TRY004
        return fragment, nodes, edges, state

    @staticmethod
    def _closed_graph_boundary(
        fragment: Mapping[str, Any],
        nodes: Mapping[str, Any],
    ) -> tuple[frozenset[str], str, dict[str, str]]:
        local_names = frozenset(nodes)
        if len(local_names) > 1_000:
            raise ValueError("graph fragment exceeds the 1000-node expansion cap")
        if not local_names or any(
            not isinstance(name, str) or not name for name in local_names
        ):
            raise ValueError("graph fragment nodes must be a non-empty string mapping")
        entry = fragment.get("entry")
        exits = fragment.get("exits")
        if (
            entry not in local_names
            or not isinstance(exits, dict)
            or "pass" not in exits
        ):
            raise ValueError("graph fragment requires an existing entry and pass exit")
        if not exits or set(exits) - {"pass", "fail", "error"}:
            raise ValueError("graph fragment exits are not closed")
        if any(target not in local_names for target in exits.values()):
            raise ValueError("graph fragment exit targets must exist")
        return local_names, entry, exits
