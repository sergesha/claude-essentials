"""Honest static structural estimates with explicit unavailable semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from lockstep.recipe.authority import StrictRecipeIngress
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor, ScopeDescriptor

from .ir import (
    CallIR,
    ChooseIR,
    EscalateIR,
    GraphIR,
    ParallelIR,
    RepeatIR,
    StepIR,
    VerifyIR,
    WorkflowIR,
)
from .semantics import WorkflowCatalog


@dataclass(frozen=True)
class ControlledTimeEstimate:
    available: bool
    upper_bound_seconds: int | None
    formula: str | None
    assumptions: tuple[str, ...]
    unavailable_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "upper_bound_seconds": self.upper_bound_seconds,
            "formula": self.formula,
            "assumptions": list(self.assumptions),
            "unavailable_reasons": list(self.unavailable_reasons),
        }


@dataclass(frozen=True)
class StructuralEstimate:
    user_work_steps: int
    maximum_validator_submissions: int
    pinned_commands: int
    child_calls: int
    maximum_child_calls: int
    peak_parallel_branches: int
    peak_parallel_child_calls: int
    maximum_runner_timeout_seconds: int | None
    generated_node_count: int
    expanded_fragment_count: int
    controlled_time: ControlledTimeEstimate

    def to_dict(self) -> dict[str, Any]:
        unavailable_resource = {
            "available": False,
            "reason": "owner-controlled runner metadata is unavailable",
            "assumptions": [],
        }
        return {
            "schema": "lockstep.structural-estimate/v1",
            "user_work_steps": self.user_work_steps,
            "maximum_validator_submissions": self.maximum_validator_submissions,
            "pinned_commands": self.pinned_commands,
            "child_calls": self.child_calls,
            "maximum_child_calls": self.maximum_child_calls,
            "peak_parallel_branches": self.peak_parallel_branches,
            "peak_parallel_child_calls": self.peak_parallel_child_calls,
            "maximum_runner_timeout_seconds": self.maximum_runner_timeout_seconds,
            "generated_node_count": self.generated_node_count,
            "expanded_fragment_count": self.expanded_fragment_count,
            "controlled_time": self.controlled_time.to_dict(),
            "end_to_end_wall_time": {
                "available": False,
                "reason": "human and external-agent completion time is unbounded",
            },
            "tokens": dict(unavailable_resource),
            "money": dict(unavailable_resource),
        }


@dataclass
class _Metrics:
    user: int = 0
    submissions: int = 0
    pinned: int = 0
    child: int = 0
    max_child: int = 0
    peak_branches: int = 0
    peak_child_calls: int = 0
    max_timeout: int | None = None
    nodes: int = 0
    fragments: int = 0
    seconds: int = 0
    formula: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def add(self, other: "_Metrics") -> None:
        self.user += other.user
        self.submissions += other.submissions
        self.pinned += other.pinned
        self.child += other.child
        self.max_child += other.max_child
        self.peak_branches = max(self.peak_branches, other.peak_branches)
        self.peak_child_calls = max(self.peak_child_calls, other.peak_child_calls)
        values = [
            item
            for item in (self.max_timeout, other.max_timeout)
            if item is not None
        ]
        self.max_timeout = max(values) if values else None
        self.nodes += other.nodes
        self.fragments += other.fragments
        self.seconds += other.seconds
        self.formula.extend(other.formula)
        self.missing.extend(reason for reason in other.missing if reason not in self.missing)


def _retry_limit(block: Any, default: Any) -> int:
    retry = getattr(block, "retry", None) or default
    return retry.limit if retry is not None else 1


def _flow(
    blocks: tuple[Any, ...], default_retry: Any, multiplier: int = 1
) -> _Metrics:
    total = _Metrics()
    for block in blocks:
        item = _block(block, default_retry, multiplier)
        total.add(item)
    return total


def _estimate_step(block: StepIR, default_retry: Any, multiplier: int) -> _Metrics:
    attempts = _retry_limit(block, default_retry) * multiplier
    return _Metrics(
        user=1,
        submissions=attempts,
        nodes=1 + (2 if attempts > multiplier else 0),
    )


def _estimate_verify(
    block: VerifyIR, default_retry: Any, multiplier: int
) -> _Metrics:
    attempts = _retry_limit(block, default_retry) * multiplier
    result = _Metrics(
        submissions=attempts,
        pinned=1,
        nodes=2 + (2 if attempts > multiplier else 0),
        max_timeout=block.timeout,
    )
    logical = block.id or "verify"
    if block.timeout is None:
        result.missing.append(f"verify '{logical}' has no timeout")
    else:
        result.seconds = block.timeout * attempts
        result.formula.append(f"verify {logical}: {block.timeout}s × {attempts}")
    return result


def _estimate_call(block: CallIR, multiplier: int) -> _Metrics:
    seconds = block.timeout_minutes * 60 if block.timeout_minutes is not None else None
    result = _Metrics(child=1, max_child=multiplier, max_timeout=seconds, nodes=1)
    logical = block.id or block.workflow
    if seconds is None:
        result.missing.append(f"child call '{logical}' has no timeout")
    else:
        result.seconds = seconds * multiplier
        result.formula.append(f"child {logical}: {seconds}s × {multiplier}")
    return result


def _estimate_choose(
    block: ChooseIR, default_retry: Any, multiplier: int
) -> _Metrics:
    branches = [_flow(items, default_retry, multiplier) for items in block.cases.values()]
    if block.default is not None:
        branches.append(_flow(block.default, default_retry, multiplier))
    result = _Metrics(nodes=1)
    if not branches:
        return result
    result.user = sum(item.user for item in branches)
    result.pinned = sum(item.pinned for item in branches)
    result.child = sum(item.child for item in branches)
    result.submissions = max(item.submissions for item in branches)
    result.max_child = max(item.max_child for item in branches)
    result.peak_branches = max(item.peak_branches for item in branches)
    result.peak_child_calls = max(item.peak_child_calls for item in branches)
    timeouts = [item.max_timeout for item in branches if item.max_timeout is not None]
    result.max_timeout = max(timeouts) if timeouts else None
    result.seconds = max(item.seconds for item in branches)
    result.missing = list(
        dict.fromkeys(reason for item in branches for reason in item.missing)
    )
    branch_formula = ", ".join(" + ".join(item.formula) or "0s" for item in branches)
    result.formula.append(f"choose max({branch_formula})")
    result.nodes += sum(item.nodes for item in branches) + 1
    return result


def _estimate_parallel(
    block: ParallelIR, default_retry: Any, multiplier: int
) -> _Metrics:
    branches = [_flow(items, default_retry, multiplier) for items in block.branches.values()]
    result = _Metrics(nodes=1)
    result.peak_branches = max(
        len(branches), max((item.peak_branches for item in branches), default=0)
    )
    result.peak_child_calls = sum(
        max(item.peak_child_calls, int(item.max_child > 0)) for item in branches
    )
    result.user = sum(item.user for item in branches)
    result.submissions = sum(item.submissions for item in branches)
    result.pinned = sum(item.pinned for item in branches)
    result.child = sum(item.child for item in branches)
    result.max_child = sum(item.max_child for item in branches)
    result.nodes += sum(item.nodes for item in branches) + 1
    result.fragments += sum(item.fragments for item in branches)
    values = [item.max_timeout for item in branches if item.max_timeout is not None]
    result.max_timeout = max(values) if values else None
    for item in branches:
        result.missing.extend(reason for reason in item.missing if reason not in result.missing)
    scope = (
        block.timeout_minutes * 60 * multiplier
        if block.timeout_minutes is not None
        else None
    )
    branch_seconds = max((item.seconds for item in branches), default=0)
    if scope is None:
        result.missing.append(f"parallel '{block.id or 'parallel'}' has no timeout")
        result.seconds = branch_seconds
    else:
        result.seconds = min(scope, branch_seconds) if branch_seconds else scope
        joined = ", ".join(f"{item.seconds}s" for item in branches)
        result.formula.append(
            f"parallel {block.id or 'parallel'}: max({joined}), "
            f"scope {block.timeout_minutes * 60}s × {multiplier}"
        )
    return result


def _block(block: Any, default_retry: Any, multiplier: int) -> _Metrics:
    if isinstance(block, StepIR):
        return _estimate_step(block, default_retry, multiplier)
    if isinstance(block, VerifyIR):
        return _estimate_verify(block, default_retry, multiplier)
    if isinstance(block, CallIR):
        return _estimate_call(block, multiplier)
    if isinstance(block, RepeatIR):
        nested = _flow(block.do, default_retry, multiplier * block.limit)
        nested.nodes += 2
        return nested
    if isinstance(block, ChooseIR):
        return _estimate_choose(block, default_retry, multiplier)
    if isinstance(block, ParallelIR):
        return _estimate_parallel(block, default_retry, multiplier)
    if isinstance(block, GraphIR):
        return _Metrics(nodes=1, fragments=1)
    if isinstance(block, EscalateIR):
        return _Metrics(nodes=0)
    return _Metrics(nodes=1)


def _structural(metrics: _Metrics) -> StructuralEstimate:
    controlled = ControlledTimeEstimate(
        not metrics.missing,
        None if metrics.missing else metrics.seconds,
        None if metrics.missing else (" + ".join(metrics.formula) or "0s"),
        () if metrics.missing else ("configured runner timeouts are enforced",),
        tuple(metrics.missing),
    )
    return StructuralEstimate(
        metrics.user, metrics.submissions, metrics.pinned, metrics.child,
        metrics.max_child, metrics.peak_branches, metrics.peak_child_calls,
        metrics.max_timeout, metrics.nodes + 4, metrics.fragments, controlled,
    )


def estimate_workflow(workflow: WorkflowIR, catalog: WorkflowCatalog) -> StructuralEstimate:
    del catalog
    if not isinstance(workflow, WorkflowIR):
        raise TypeError("estimate_workflow requires WorkflowIR")
    return _structural(_flow(workflow.flow, workflow.defaults.retry))


@dataclass(frozen=True)
class _ManualGraphAnalysis:
    nodes: Mapping[str, Any]
    adjacency: Mapping[str, frozenset[str]]
    unconditional: Mapping[str, frozenset[str]]
    loop_limits: Mapping[str, Any]

    def reachable(self, source_name: str, target_name: str) -> bool:
        pending = [source_name]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target_name:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self.adjacency.get(current, ()))
        return False

    def executions(self, node_name: str) -> int:
        multiplier = 1
        for capped_name, limit in self.loop_limits.items():
            if (
                type(limit) is int
                and limit > 0
                and self.reachable(capped_name, node_name)
                and self.reachable(node_name, capped_name)
            ):
                multiplier *= limit
        return multiplier

    def peak_parallel_branches(self) -> int:
        return max(
            (len(targets) for targets in self.unconditional.values() if len(targets) > 1),
            default=0,
        )

    def peak_parallel_child_calls(self) -> int:
        return max(
            (
                sum(
                    int(
                        isinstance(self.nodes.get(target), Mapping)
                        and self.nodes[target].get("type") == "subgraph"
                    )
                    for target in targets
                )
                for targets in self.unconditional.values()
                if len(targets) > 1
            ),
            default=0,
        )


def _manual_graph_analysis(document: Mapping[str, Any]) -> _ManualGraphAnalysis:
    nodes = document.get("nodes") or {}
    adjacency: dict[str, set[str]] = {}
    unconditional: dict[str, set[str]] = {}
    for edge in document.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        source_name, raw_targets = edge.get("from"), edge.get("to")
        targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
        if isinstance(source_name, str):
            for target_name in targets:
                if not isinstance(target_name, str):
                    continue
                adjacency.setdefault(source_name, set()).add(target_name)
                if "condition" not in edge:
                    unconditional.setdefault(source_name, set()).add(target_name)
    return _ManualGraphAnalysis(
        nodes=nodes,
        adjacency={key: frozenset(value) for key, value in adjacency.items()},
        unconditional={key: frozenset(value) for key, value in unconditional.items()},
        loop_limits=document.get("loop_limits") or {},
    )


def _manual_node_metrics(
    node_name: str,
    node: Any,
    analysis: _ManualGraphAnalysis,
    known_state_keys: set[str],
) -> _Metrics:
    if not isinstance(node, Mapping):
        return _Metrics(nodes=0)
    if node.get("type") == "subgraph":
        return _Metrics(
            child=1,
            max_child=analysis.executions(node_name),
            nodes=0,
            missing=[f"subgraph call '{node_name}' has no timeout"],
        )
    if node.get("type") != "interrupt":
        return _Metrics(nodes=0)
    raw_descriptor = (node.get("message") or {}).get("lockstep_effect")
    if not isinstance(raw_descriptor, Mapping):
        return _Metrics(nodes=0)
    descriptor = parse_effect_descriptor(
        raw_descriptor, known_state_keys=known_state_keys
    )
    if isinstance(descriptor, ScopeDescriptor):
        raise ValueError(
            "manual recipe estimate does not admit compiler-only scope descriptors"
        )
    if not isinstance(descriptor, EffectDescriptor):
        return _Metrics(nodes=0)
    executions = analysis.executions(node_name)
    if descriptor.kind == "manual":
        return _Metrics(user=1, submissions=executions, nodes=0)
    if descriptor.kind not in {"verify", "pinned"}:
        return _Metrics(nodes=0)
    timeout = descriptor.deadline_seconds
    if type(timeout) is not int:
        return _Metrics(
            pinned=1,
            submissions=executions,
            nodes=0,
            missing=[f"protected effect '{descriptor.logical_id}' has no timeout"],
        )
    return _Metrics(
        pinned=1,
        submissions=executions,
        max_timeout=timeout,
        nodes=0,
        seconds=timeout * executions,
        formula=[f"protected {descriptor.logical_id}: {timeout}s × {executions}"],
    )


def _manual_recipe_metrics(
    document: Mapping[str, Any], analysis: _ManualGraphAnalysis
) -> _Metrics:
    metrics = _Metrics(nodes=len(analysis.nodes))
    known_state_keys = set(document.get("state") or {})
    for node_name, node in analysis.nodes.items():
        metrics.add(
            _manual_node_metrics(
                str(node_name), node, analysis, known_state_keys
            )
        )
    return metrics


def _manual_structural_estimate(
    metrics: _Metrics,
    analysis: _ManualGraphAnalysis,
    expanded_fragment_count: int,
) -> StructuralEstimate:
    controlled = ControlledTimeEstimate(
        not metrics.missing,
        metrics.seconds if not metrics.missing else None,
        (" + ".join(metrics.formula) or "0s") if not metrics.missing else None,
        ("configured runner timeouts are enforced",) if not metrics.missing else (),
        tuple(metrics.missing),
    )
    return StructuralEstimate(
        metrics.user,
        metrics.submissions,
        metrics.pinned,
        metrics.child,
        metrics.max_child,
        analysis.peak_parallel_branches(),
        analysis.peak_parallel_child_calls(),
        metrics.max_timeout,
        metrics.nodes,
        expanded_fragment_count,
        controlled,
    )


def estimate_manual_recipe(path: str | Path) -> StructuralEstimate:
    source = Path(path)
    candidate = StrictRecipeIngress(source.parent).inspect(source.name)
    root = next(item for item in candidate.files if item.path == source.name)
    document = yaml.safe_load(root.bytes) or {}
    analysis = _manual_graph_analysis(document)
    metrics = _manual_recipe_metrics(document, analysis)
    return _manual_structural_estimate(
        metrics, analysis, max(0, len(candidate.files) - 1)
    )
