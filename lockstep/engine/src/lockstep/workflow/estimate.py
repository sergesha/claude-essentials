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
    peak_parallel_subcalls: int
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
            "peak_parallel_subcalls": self.peak_parallel_subcalls,
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
    peak_subcalls: int = 0
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
        self.peak_subcalls = max(self.peak_subcalls, other.peak_subcalls)
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


def _block(block: Any, default_retry: Any, multiplier: int) -> _Metrics:
    result = _Metrics(nodes=1)
    if isinstance(block, StepIR):
        attempts = _retry_limit(block, default_retry) * multiplier
        result.user = 1
        result.submissions = attempts
        result.nodes = 1 + (2 if attempts > multiplier else 0)
    elif isinstance(block, VerifyIR):
        attempts = _retry_limit(block, default_retry) * multiplier
        result.submissions = attempts
        result.pinned = 1
        result.nodes = 2 + (2 if attempts > multiplier else 0)
        result.max_timeout = block.timeout
        logical = block.id or "verify"
        if block.timeout is None:
            result.missing.append(f"verify '{logical}' has no timeout")
        else:
            result.seconds = block.timeout * attempts
            result.formula.append(f"verify {logical}: {block.timeout}s × {attempts}")
    elif isinstance(block, CallIR):
        result.child = 1
        result.max_child = multiplier
        seconds = (
            block.timeout_minutes * 60
            if block.timeout_minutes is not None
            else None
        )
        result.max_timeout = seconds
        logical = block.id or block.workflow
        if seconds is None:
            result.missing.append(f"child call '{logical}' has no timeout")
        else:
            result.seconds = seconds * multiplier
            result.formula.append(f"child {logical}: {seconds}s × {multiplier}")
    elif isinstance(block, RepeatIR):
        nested = _flow(block.do, default_retry, multiplier * block.limit)
        nested.nodes += 2
        return nested
    elif isinstance(block, ChooseIR):
        branches = [
            _flow(items, default_retry, multiplier)
            for items in block.cases.values()
        ]
        if block.default is not None:
            branches.append(_flow(block.default, default_retry, multiplier))
        if branches:
            result.user = sum(item.user for item in branches)
            result.pinned = sum(item.pinned for item in branches)
            result.child = sum(item.child for item in branches)
            result.submissions = max(item.submissions for item in branches)
            result.max_child = max(item.max_child for item in branches)
            result.peak_branches = max(item.peak_branches for item in branches)
            result.peak_subcalls = max(item.peak_subcalls for item in branches)
            timeouts = [
                item.max_timeout
                for item in branches
                if item.max_timeout is not None
            ]
            result.max_timeout = max(timeouts) if timeouts else None
            result.seconds = max(item.seconds for item in branches)
            result.missing = list(
                dict.fromkeys(reason for item in branches for reason in item.missing)
            )
            branch_formula = ", ".join(
                " + ".join(item.formula) or "0s" for item in branches
            )
            result.formula.append(f"choose max({branch_formula})")
            result.nodes += sum(item.nodes for item in branches) + 1
    elif isinstance(block, ParallelIR):
        branches = [
            _flow(items, default_retry, multiplier)
            for items in block.branches.values()
        ]
        result.peak_branches = max(
            len(branches),
            max((item.peak_branches for item in branches), default=0),
        )
        result.peak_subcalls = sum(
            max(item.peak_subcalls, int(item.max_child > 0)) for item in branches
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
            result.missing.extend(
                reason for reason in item.missing if reason not in result.missing
            )
        scope = (
            block.timeout_minutes * 60 * multiplier
            if block.timeout_minutes is not None
            else None
        )
        branch_seconds = max((item.seconds for item in branches), default=0)
        if scope is None:
            result.missing.append(
                f"parallel '{block.id or 'parallel'}' has no timeout"
            )
            result.seconds = branch_seconds
        else:
            result.seconds = min(scope, branch_seconds) if branch_seconds else scope
            joined = ", ".join(f"{item.seconds}s" for item in branches)
            result.formula.append(
                f"parallel {block.id or 'parallel'}: max({joined}), "
                f"scope {block.timeout_minutes * 60}s × {multiplier}"
            )
    elif isinstance(block, GraphIR):
        result.fragments = 1
    elif isinstance(block, EscalateIR):
        result.nodes = 0
    return result


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
        metrics.max_child, metrics.peak_branches, metrics.peak_subcalls,
        metrics.max_timeout, metrics.nodes + 4, metrics.fragments, controlled,
    )


def estimate_workflow(workflow: WorkflowIR, catalog: WorkflowCatalog) -> StructuralEstimate:
    del catalog
    if not isinstance(workflow, WorkflowIR):
        raise TypeError("estimate_workflow requires WorkflowIR")
    return _structural(_flow(workflow.flow, workflow.defaults.retry))


def estimate_manual_recipe(path: str | Path) -> StructuralEstimate:
    source = Path(path)
    candidate = StrictRecipeIngress(source.parent).inspect(source.name)
    root = next(item for item in candidate.files if item.path == source.name)
    document = yaml.safe_load(root.bytes) or {}
    nodes = document.get("nodes") or {}
    edges = document.get("edges") or []
    adjacency: dict[str, set[str]] = {}
    unconditional: dict[str, set[str]] = {}
    for edge in edges:
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

    def reachable(source_name: str, target_name: str) -> bool:
        pending = [source_name]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target_name:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(adjacency.get(current, ()))
        return False

    loop_limits = document.get("loop_limits") or {}

    def executions(node_name: str) -> int:
        multiplier = 1
        for capped_name, limit in loop_limits.items():
            if (
                type(limit) is int
                and limit > 0
                and reachable(capped_name, node_name)
                and reachable(node_name, capped_name)
            ):
                multiplier *= limit
        return multiplier

    user = pinned = submissions = 0
    child_calls = maximum_child_calls = 0
    max_timeout: int | None = None
    controlled_seconds = 0
    formulas: list[str] = []
    missing: list[str] = []
    for node_name, node in nodes.items():
        if not isinstance(node, Mapping) or node.get("type") != "interrupt":
            if isinstance(node, Mapping) and node.get("type") == "subgraph":
                child_calls += 1
                maximum_child_calls += executions(str(node_name))
                missing.append(f"subgraph call '{node_name}' has no timeout")
            continue
        raw_descriptor = (node.get("message") or {}).get("lockstep_effect")
        if not isinstance(raw_descriptor, Mapping):
            continue
        descriptor = parse_effect_descriptor(
            raw_descriptor, known_state_keys=set(document.get("state") or {})
        )
        if isinstance(descriptor, ScopeDescriptor):
            raise ValueError(
                "manual recipe estimate does not admit compiler-only scope descriptors"
            )
        if not isinstance(descriptor, EffectDescriptor):
            continue
        kind = descriptor.kind
        if kind == "manual":
            user += 1
            submissions += executions(str(node_name))
        elif kind in {"verify", "pinned"}:
            pinned += 1
            effect_executions = executions(str(node_name))
            submissions += effect_executions
            timeout = descriptor.deadline_seconds
            if type(timeout) is int:
                max_timeout = max(max_timeout or 0, timeout)
                controlled_seconds += timeout * effect_executions
                formulas.append(
                    f"protected {descriptor.logical_id}: "
                    f"{timeout}s × {effect_executions}"
                )
            else:
                missing.append(
                    f"protected effect '{descriptor.logical_id}' has no timeout"
                )
    controlled = ControlledTimeEstimate(
        not missing,
        controlled_seconds if not missing else None,
        (" + ".join(formulas) or "0s") if not missing else None,
        ("configured runner timeouts are enforced",) if not missing else (),
        tuple(missing),
    )
    return StructuralEstimate(
        user,
        submissions,
        pinned,
        child_calls,
        maximum_child_calls,
        max(
            (len(targets) for targets in unconditional.values() if len(targets) > 1),
            default=0,
        ),
        max(
            (
                sum(
                    int(isinstance(nodes.get(target), Mapping) and nodes[target].get("type") == "subgraph")
                    for target in targets
                )
                for targets in unconditional.values()
                if len(targets) > 1
            ),
            default=0,
        ),
        max_timeout,
        len(nodes),
        max(0, len(candidate.files) - 1),
        controlled,
    )
