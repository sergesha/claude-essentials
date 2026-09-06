"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import hashlib
from typing import Mapping  # noqa: UP035 - preserves existing hints

from ._lowering_contracts import _Exit, _Fragment
from ._semantics_contracts import EffectContract
from ._semantics_parallel import manual_effects
from .ir import ParallelIR
from .semantics import BlockContract


class _LoweringParallel:
    def _parallel_scope_fragment(
        self,
        block: ParallelIR,
        pointer: str,
        digest: str,
        outer_scopes: tuple[str, ...],
    ) -> tuple[_Fragment | None, tuple[str, ...]]:
        if block.timeout_minutes is not None:
            scope_key = f"parallel_{digest}_scope_result"
            descriptor = {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": f"parallel-{digest}-scope",
                "scope_kind": "parallel",
                "duration_seconds": block.timeout_minutes * 60,
                "runner_selector": None,
                "ancestor_deadline_state_keys": list(outer_scopes),
                "result_state_key": scope_key,
                "result_schema": "lockstep.scope-result/v1",
            }
            scope_fragment = self.descriptor_interrupt(
                pointer,
                "parallel",
                f"parallel-{digest}-scope",
                descriptor,
                {"step": block.id, "lockstep_effect": descriptor},
                scope_key,
                None,
            )
            return scope_fragment, (*outer_scopes, scope_key)
        return None, outer_scopes

    def _lower_parallel_branches(
        self,
        *,
        contract: BlockContract,
        pointer: str,
        digest: str,
        join: str,
        branch_scopes: tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        outer_targets = dict(self.outcome_targets)
        outer_scopes = self.active_scope_state_keys
        outer_aborted_capture = self.capture_aborted_effects
        outer_parallel_branch = self.inside_parallel_branch
        outer_manual_parallel = self.manual_parallel
        parallel_writes = list(EffectContract().union(*(
            manual_effects(branch, include_artifacts=True)
            for branch in contract.branches.values()
        )).writes)
        branch_entries: list[str] = []
        branch_result_keys: list[str] = []
        branch_completions: list[str] = []
        try:
            for branch_name, branch_flow in contract.branches.items():
                branch_pointer = f"{pointer}/parallel/branches/{branch_name}"
                branch_key = (
                    f"parallel_{digest}_{branch_name.replace('-', '_')}_outcome"
                )
                self.declare_generated_state(branch_key, "str")
                branch_result_keys.append(branch_key)
                completion = self.node(
                    branch_pointer,
                    "parallel-branch",
                    "complete",
                    {"type": "passthrough"},
                )
                setters = {
                    outcome: self.node(
                        branch_pointer,
                        "parallel-branch",
                        f"set-{outcome.lower()}",
                        {"type": "passthrough", "output": {branch_key: outcome}},
                    )
                    for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
                }
                for setter in setters.values():
                    self.edge(setter, completion)
                branch_completions.append(completion)

                self.active_scope_state_keys = branch_scopes
                self.outcome_targets = setters
                self.capture_aborted_effects = True
                self.inside_parallel_branch = True
                self.manual_parallel = {
                    "id": contract.block.id,
                    "branch": branch_name,
                    "writes": parallel_writes,
                }
                fragment = self.flow_contract(branch_flow, branch_pointer)
                branch_entries.append(fragment.entry)
                self.connect(fragment.exits, setters["PASS"])
        finally:
            self.active_scope_state_keys = outer_scopes
            self.outcome_targets = outer_targets
            self.capture_aborted_effects = outer_aborted_capture
            self.inside_parallel_branch = outer_parallel_branch
            self.manual_parallel = outer_manual_parallel
        self.edge(branch_completions, join)
        return branch_entries, branch_result_keys

    def _route_parallel_outcomes(
        self,
        *,
        pointer: str,
        join: str,
        result_key: str,
        branch_result_keys: list[str],
        outer_targets: Mapping[str, str],
    ) -> str:
        aggregate = {
            "PASS": {"outcome": "PASS", "value": "pass"},
            "FAIL": {"outcome": "FAIL", "value": "fail"},
            "ERROR": {"outcome": "ERROR", "value": "error"},
            "ABORTED": {
                "outcome": "ERROR",
                "value": "error",
                "fixed_error_code": "cancelled",
            },
        }
        outcomes = {
            outcome: self.node(
                pointer,
                "parallel",
                f"outcome-{outcome.lower()}",
                {"type": "passthrough", "output": {result_key: value}},
            )
            for outcome, value in aggregate.items()
        }
        route = join
        for precedence in ("ABORTED", "ERROR", "FAIL"):
            for index, branch_key in enumerate(branch_result_keys):
                next_route = self.node(
                    pointer,
                    "parallel",
                    f"check-{precedence.lower()}-{index}",
                    {"type": "passthrough"},
                )
                self.edge(
                    route, outcomes[precedence], f"{branch_key} == '{precedence}'"
                )
                self.edge(route, next_route, f"{branch_key} != '{precedence}'")
                route = next_route
        self.edge(route, outcomes["PASS"])
        for outcome in ("FAIL", "ERROR", "ABORTED"):
            self.edge(outcomes[outcome], outer_targets[outcome])
        return outcomes["PASS"]

    def parallel(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, ParallelIR):
            raise TypeError("parallel lowering requires ParallelIR")
        if block.id is None or block.join != "all":
            raise ValueError("parallel lowering requires an id and join: all")

        outer_targets = dict(self.outcome_targets)
        outer_scopes = self.active_scope_state_keys
        digest = hashlib.sha256(
            b"lockstep.parallel-scope/v1\0" + pointer.encode("utf-8")
        ).hexdigest()[:24]
        scope_fragment, branch_scopes = self._parallel_scope_fragment(
            block, pointer, digest, outer_scopes
        )

        fork = self.node(pointer, "parallel", "fork", {"type": "passthrough"})
        join = self.node(pointer, "parallel", "join", {"type": "passthrough"})
        result_key = f"{block.id.replace('-', '_')}_result"
        self.declare_generated_state(result_key, "dict")
        self.outcome_keys[block.id] = result_key

        branch_entries, branch_result_keys = self._lower_parallel_branches(
            contract=contract,
            pointer=pointer,
            digest=digest,
            join=join,
            branch_scopes=branch_scopes,
        )

        self.edge(fork, branch_entries)
        if scope_fragment is None:
            entry = fork
        else:
            self.connect(scope_fragment.exits, fork)
            entry = scope_fragment.entry

        pass_outcome = self._route_parallel_outcomes(
            pointer=pointer,
            join=join,
            result_key=result_key,
            branch_result_keys=branch_result_keys,
            outer_targets=outer_targets,
        )
        return _Fragment(entry, [_Exit(pass_outcome)])
