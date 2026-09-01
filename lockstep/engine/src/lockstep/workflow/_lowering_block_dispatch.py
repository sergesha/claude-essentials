"""Thin orchestration owner for workflow lowering."""

from __future__ import annotations

from ._lowering_contracts import _Fragment
from .ir import (
    AcceptIR,
    CallIR,
    ChooseIR,
    DecideIR,
    EscalateIR,
    GraphIR,
    ParallelIR,
    StepIR,
    VerifyIR,
)
from .semantics import BlockContract


class _LoweringBlockDispatch:
    def block(
        self,
        contract: BlockContract,
        pointer: str,
        *,
        failure_target: str | None = None,
    ) -> _Fragment:
        block = contract.block
        retry_limit = contract.retry.limit if contract.retry else None
        if isinstance(block, StepIR):
            return self._lower_step(block, pointer, retry_limit, failure_target)
        if isinstance(block, VerifyIR):
            return self._lower_verify(block, pointer, retry_limit, failure_target)
        if isinstance(block, DecideIR):
            return self._lower_decide(block, pointer)
        if isinstance(block, AcceptIR):
            return self._lower_accept(block, pointer)
        if isinstance(block, EscalateIR):
            return _Fragment(self.outcome_target("FAIL"), [])
        if isinstance(block, ChooseIR):
            return self._lower_choose(block, contract, pointer)
        if isinstance(block, GraphIR):
            return self.graph(contract, pointer)
        if isinstance(block, CallIR):
            return self.call(contract, pointer)
        if isinstance(block, ParallelIR):
            return self.parallel(contract, pointer)
        raise NotImplementedError(f"Task 8 cannot lower {type(block).__name__}")
