"""Ordered graph-fragment node installation."""

from __future__ import annotations

from typing import Any

from ._lowering_contracts import _FragmentNames, _GraphFragmentPlan
from .canonical import plain


def protected_fragment_resume_keys(plan: _GraphFragmentPlan) -> set[Any]:
    return {
        node.get("resume_key")
        for node in plan.nodes.values()
        if isinstance(node, dict) and node.get("type") == "interrupt"
    }


def prepare_fragment_node(
    builder: Any,
    *,
    name: str,
    node: Any,
    plan: _GraphFragmentPlan,
    names: _FragmentNames,
    protected_resume_keys: set[Any],
    fragment_state_keys: set[str],
) -> tuple[dict[str, Any], tuple[str, tuple[str, ...]] | None, tuple[str, ...]]:
    if not isinstance(node, dict) or node.get("type") not in {
        "passthrough",
        "interrupt",
    }:
        raise ValueError(
            "generated graph fragments may contain only passthrough and "
            "protected interrupt nodes"
        )
    copied = plain(node)
    outcome = None
    writes: tuple[str, ...] = ()
    if copied.get("type") == "interrupt":
        resume_key, outcomes, writes = builder._rewrite_fragment_interrupt(
            copied, node, names, fragment_state_keys
        )
        outcome = (resume_key, outcomes)
    builder._rewrite_fragment_output(
        copied,
        protected_resume_keys=protected_resume_keys,
        plan=plan,
        names=names,
    )
    return copied, outcome, writes


def store_fragment_node(
    builder: Any,
    *,
    name: str,
    copied: dict[str, Any],
    names: _FragmentNames,
    pointer: str,
) -> None:
    qualified = names.node(name)
    if qualified in builder.nodes:
        raise ValueError(f"fragment node collision: {qualified}")
    builder.nodes[qualified] = copied
    mark = builder.workflow.location_for(pointer)
    builder.source_nodes[qualified] = {
        "pointer": pointer,
        "line": mark.line if mark else 1,
        "column": mark.column if mark else 1,
    }
