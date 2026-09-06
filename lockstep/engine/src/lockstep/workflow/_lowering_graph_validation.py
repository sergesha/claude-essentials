"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import re
from typing import Any, Mapping  # noqa: UP035 - preserves existing hints

from ._lowering_conditions import _condition_may_match_outcome
from ._lowering_contracts import _FragmentNames, _GraphFragmentPlan


class _LoweringGraphValidation:
    @staticmethod
    def _validate_fragment_effects(
        fragment: Mapping[str, Any],
        declared_writes: list[str],
    ) -> None:
        effects = fragment.get("effects", {})
        mode = effects.get("mode") if isinstance(effects, dict) else None
        expected_writes = (
            list(effects.get("writes", [])) if isinstance(effects, dict) else []
        )
        if mode not in {"read-only", "declared-writes"}:
            raise ValueError("fragment effects mode must be closed")
        if mode == "read-only" and (expected_writes or declared_writes):
            raise ValueError("read-only fragment may not declare protected writes")
        canonical_writes = sorted(set(declared_writes))
        if expected_writes != sorted(set(expected_writes)):
            raise ValueError("fragment declared writes must be canonical and unique")
        if expected_writes != canonical_writes:
            raise ValueError(
                "fragment declared writes must exactly equal protected "
                "descriptor writes"
            )

    def _install_fragment_edges(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> tuple[dict[str, set[str]], dict[str, list[dict[str, Any]]]]:
        adjacency: dict[str, set[str]] = {name: set() for name in plan.local_names}
        edges_by_source: dict[str, list[dict[str, Any]]] = {
            name: [] for name in plan.local_names
        }
        for edge in plan.edges:
            if not isinstance(edge, dict) or set(edge) - {"from", "to", "condition"}:
                raise ValueError("invalid graph fragment edge")
            source, target = edge.get("from"), edge.get("to")
            sources = source if isinstance(source, list) else [source]
            if any(item not in plan.local_names for item in sources) or target not in plan.local_names:
                raise ValueError("graph fragment edges must remain inside the fragment")
            for item in sources:
                adjacency[item].add(target)
                edges_by_source[item].append(edge)
            self.edge(
                [names.node(item) for item in sources] if isinstance(source, list) else names.node(source),
                names.node(target),
                names.condition(edge.get("condition")),
            )
        return adjacency, edges_by_source

    @staticmethod
    def _fragment_conditions_are_exhaustive(
        source: str,
        conditions: list[str],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> bool:
        outcome_contract = interrupt_outcomes.get(source)
        if outcome_contract is not None:
            result_key, outcomes = outcome_contract
            covered = set()
            for condition in conditions:
                match = re.fullmatch(
                    rf"\s*(?:state\.)?{re.escape(result_key)}\.outcome\s*==\s*"
                    r"(['\"])(PASS|FAIL|ERROR)\1\s*",
                    condition,
                )
                if match is not None:
                    covered.add(match.group(2).lower())
            if covered >= set(outcomes):
                return True
        comparisons: list[tuple[str, str, str]] = []
        for condition in conditions:
            match = re.fullmatch(
                r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*"
                r"(==|!=|<=|>=|<|>)\s*(.+?)\s*",
                condition,
            )
            if match is not None:
                comparisons.append(match.groups())
        complements = {"==": "!=", "!=": "=="}
        return any(
            left == other_left
            and value == other_value
            and operator in complements
            and complements[operator] == other_operator
            for left, operator, value in comparisons
            for other_left, other_operator, other_value in comparisons
        )

    def _validate_fragment_edge_routes(
        self,
        edges_by_source: Mapping[str, list[dict[str, Any]]],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> None:
        for source, outgoing in edges_by_source.items():
            conditional = [
                edge for edge in outgoing if isinstance(edge.get("condition"), str)
            ]
            if not conditional:
                continue
            if len(conditional) != len(outgoing):
                raise ValueError(
                    "fragment nodes may not mix conditional and unconditional edges"
                )
            conditions = [edge["condition"] for edge in conditional]
            if self._fragment_conditions_are_exhaustive(
                source, conditions, interrupt_outcomes
            ):
                continue
            raise ValueError("fragment conditional routing must be proven exhaustive")

    @staticmethod
    def _fragment_loop_analysis(
        plan: _GraphFragmentPlan,
        adjacency: Mapping[str, set[str]],
    ) -> tuple[dict[str, int], dict[str, str], dict[str, set[str]]]:
        loop_limits = plan.raw.get("loop_limits", {})
        loop_exits = plan.raw.get("loop_exits", {})
        if not isinstance(loop_limits, dict) or not isinstance(loop_exits, dict):
            raise ValueError("fragment loop metadata must be mappings")  # noqa: TRY004
        if set(loop_limits) != set(loop_exits):
            raise ValueError("fragment loop limits and exits must name the same nodes")
        if any(
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
            for limit in loop_limits.values()
        ):
            raise ValueError("fragment loop limits must be positive integers")
        analysis_adjacency = {name: set(targets) for name, targets in adjacency.items()}
        for local_name, exit_target in loop_exits.items():
            if (
                local_name not in plan.local_names
                or exit_target not in plan.local_names
            ):
                raise ValueError("fragment loop exit references an unknown node")
            if exit_target not in plan.exits.values():
                raise ValueError("fragment loop exit must target a declared local exit")
            analysis_adjacency[local_name].add(exit_target)
        return loop_limits, loop_exits, analysis_adjacency

    @staticmethod
    def _effect_outcome_reachability(
        *,
        interrupt_name: str,
        resume_key: str,
        outcome_name: str,
        edge_records: list[dict[str, Any]],
        loop_exits: Mapping[str, str],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> tuple[set[str], bool]:
        reachable: set[str] = set()
        pending = [interrupt_name]
        reached_successor_effect = False
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            if current != interrupt_name and current in interrupt_outcomes:
                reached_successor_effect = True
                continue
            possible_targets = {
                edge["to"]
                for edge in edge_records
                if current in (edge["from"] if isinstance(edge.get("from"), list) else [edge.get("from")])
                and _condition_may_match_outcome(
                    edge.get("condition"), resume_key, outcome_name.upper()
                )
            }
            if current in loop_exits:
                possible_targets.add(loop_exits[current])
            if current == interrupt_name and len(possible_targets) != 1:
                raise ValueError(
                    "protected fragment effect routing must select exactly one "
                    "successor for every outcome"
                )
            pending.extend(possible_targets)
        return reachable, reached_successor_effect

    def _validate_fragment_effect_outcomes(
        self,
        plan: _GraphFragmentPlan,
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
        loop_exits: Mapping[str, str],
    ) -> None:
        edge_records = [edge for edge in plan.edges if isinstance(edge, dict)]
        for interrupt_name, (resume_key, outcomes) in interrupt_outcomes.items():
            for outcome_name in outcomes:
                if outcome_name not in plan.exits:
                    raise ValueError(
                        f"fallible fragment effect requires a declared "
                        f"{outcome_name} exit"
                    )
                reachable, reached_successor = self._effect_outcome_reachability(
                    interrupt_name=interrupt_name,
                    resume_key=resume_key,
                    outcome_name=outcome_name,
                    edge_records=edge_records,
                    loop_exits=loop_exits,
                    interrupt_outcomes=interrupt_outcomes,
                )
                reached_exits = {
                    name for name, target in plan.exits.items() if target in reachable
                }
                pass_valid = outcome_name == "pass" and (
                    (reached_successor and not reached_exits)
                    or (not reached_successor and reached_exits == {"pass"})
                )
                failure_valid = (
                    outcome_name != "pass"
                    and not reached_successor
                    and reached_exits == {outcome_name}
                )
                if not (pass_valid or failure_valid):
                    raise ValueError(
                        "protected fragment effect outcomes must reach only their "
                        "matching declared exit"
                    )

    @staticmethod
    def _reachable_fragment_nodes(
        starts: list[str],
        adjacency: Mapping[str, set[str]],
    ) -> set[str]:
        reachable: set[str] = set()
        frontier = list(starts)
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(adjacency[current])
        return reachable

    @staticmethod
    def _fragment_termination_nodes(
        exits: Mapping[str, str],
        adjacency: Mapping[str, set[str]],
    ) -> set[str]:
        reverse: dict[str, set[str]] = {name: set() for name in adjacency}
        for source, targets in adjacency.items():
            for target in targets:
                reverse[target].add(source)
        return _LoweringGraphValidation._reachable_fragment_nodes(
            list(exits.values()), reverse
        )

    @staticmethod
    def _visit_fragment_cycle(
        name: str,
        *,
        adjacency: Mapping[str, set[str]],
        loop_limits: Mapping[str, int],
        loop_exits: Mapping[str, str],
        exits: Mapping[str, str],
        visiting: set[str],
        visited: set[str],
    ) -> None:
        if name in visiting:
            raise ValueError("unbounded graph fragment cycle is not allowed")
        if name in visited:
            return
        visiting.add(name)
        for target in adjacency[name]:
            if target in visiting:
                capped = target if target in loop_limits else name
                cap = loop_limits.get(capped)
                exit_target = loop_exits.get(capped)
                if (
                    not isinstance(cap, int)
                    or isinstance(cap, bool)
                    or cap < 1
                    or exit_target not in exits.values()
                ):
                    raise ValueError(
                        "graph fragment cycle requires a positive local limit "
                        "and declared local exit"
                    )
                continue
            _LoweringGraphValidation._visit_fragment_cycle(
                target,
                adjacency=adjacency,
                loop_limits=loop_limits,
                loop_exits=loop_exits,
                exits=exits,
                visiting=visiting,
                visited=visited,
            )
        visiting.remove(name)
        visited.add(name)

    def _validate_fragment_topology(
        self,
        *,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        adjacency: Mapping[str, set[str]],
        analysis_adjacency: Mapping[str, set[str]],
        loop_limits: Mapping[str, int],
        loop_exits: Mapping[str, str],
    ) -> None:
        reachable = self._reachable_fragment_nodes([plan.entry], analysis_adjacency)
        if any(target not in reachable for target in plan.exits.values()):
            raise ValueError("every declared graph fragment exit must be reachable")
        if reachable != plan.local_names:
            raise ValueError("graph fragment may not contain unreachable nodes")
        can_terminate = self._fragment_termination_nodes(plan.exits, analysis_adjacency)
        if reachable - can_terminate:
            raise ValueError("every reachable fragment path must be able to terminate")
        self._visit_fragment_cycle(
            plan.entry,
            adjacency=adjacency,
            loop_limits=loop_limits,
            loop_exits=loop_exits,
            exits=plan.exits,
            visiting=set(),
            visited=set(),
        )
        for local_name, limit in loop_limits.items():
            if local_name not in plan.local_names:
                raise ValueError("fragment loop limit references an unknown node")
            self.loop_limits[names.node(local_name)] = limit
        for local_name, exit_target in loop_exits.items():
            self.loop_exits[names.node(local_name)] = names.node(exit_target)
        terminal_names = {name for name in reachable if not adjacency[name]}
        if terminal_names - set(plan.exits.values()):
            raise ValueError("every reachable graph fragment path must end at an exit")
        if any(adjacency[target] for target in plan.exits.values()):
            raise ValueError("graph fragment exit nodes may not have outgoing edges")
