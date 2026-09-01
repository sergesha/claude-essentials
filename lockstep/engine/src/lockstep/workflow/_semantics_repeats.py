"""Bounded repeat validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ._semantics_contracts import (
    _ID,
    FlowContract,
    OutcomeSymbol,
    RepeatContract,
    RepeatControlContract,
    _escape,
)
from ._semantics_validation import _ValidationState, fail
from .ir import BlockIR, ChooseIR, EscalateIR, RepeatIR, VerifyIR


def repeat(
    state: _ValidationState,
    block: RepeatIR,
    pointer: str,
    symbols: Mapping[str, OutcomeSymbol],
    *,
    flow: Callable[..., FlowContract],
) -> tuple[RepeatContract, Mapping[str, OutcomeSymbol]]:
    if block.limit < 1 or block.exhausted != "escalate":
        fail(state, "LSW303", "repeat requires a positive limit and exhausted: escalate", f"{pointer}/repeat", "use a positive limit and exhausted: escalate")
    producer, suffix = repeat_target(state, block.until, pointer)
    if suffix != "passed":
        fail(state, "LSW303", "repeat.until must reference the producer's .passed outcome", f"{pointer}/repeat/until", "use <verify-id>.passed")
    if not block.do:
        fail(state, "LSW303", "repeat do must contain its terminal producer", f"{pointer}/repeat/do", "add the referenced final verify block")
    last = block.do[-1]
    if not isinstance(last, VerifyIR) or last.id != producer:
        fail(state, "LSW303", "repeat.until must name the last normally reachable verify in do", f"{pointer}/repeat/until", "make the referenced verify the final block of every iteration")
    effective_retry = last.retry or state.workflow.defaults.retry
    if effective_retry is not None:
        fail(state, "LSW303", "repeat terminal producer cannot retry", f"{pointer}/repeat/do/{len(block.do) - 1}/verify/retry", "remove retry from the terminal producer and workflow defaults")
    cardinalities = repeat_cardinalities(state, block.do, f"{pointer}/repeat/do", producer)
    if not cardinalities or any(count != 1 for count in cardinalities):
        fail(state, "LSW303", "every repeat path must execute its terminal producer exactly once", f"{pointer}/repeat/until", "make every path reconverge through the final producer exactly once")
    nested = flow(state, block.do, f"{pointer}/repeat/do", symbols, parallel=False)
    control = RepeatControlContract(producer, cardinalities)
    return RepeatContract(block.id, block.limit, block.until, block.exhausted, nested.effects, nested, control), {}


def repeat_cardinalities(
    state: _ValidationState, blocks: tuple[BlockIR, ...], pointer: str, producer: str
) -> tuple[int, ...]:
    cardinalities = (0,)
    for index, item in enumerate(blocks):
        item_pointer = f"{pointer}/{index}"
        if isinstance(item, EscalateIR):
            fail(state, "LSW303", "repeat paths cannot bypass their terminal producer", item_pointer, "remove escalation from repeat do or move it after the repeat")
        if isinstance(item, ChooseIR):
            branches = [(label, branch, False) for label, branch in item.cases.items()]
            if item.default is not None:
                branches.append(("", item.default, True))
            branch_counts: list[int] = []
            for label, branch, is_default in branches:
                branch_pointer = (
                    f"{item_pointer}/choose/default"
                    if is_default else f"{item_pointer}/choose/cases/{_escape(label)}"
                )
                branch_counts.extend(repeat_cardinalities(state, branch, branch_pointer, producer))
            cardinalities = tuple(before + count for before in cardinalities for count in branch_counts)
            continue
        increment = int(isinstance(item, VerifyIR) and item.id == producer)
        cardinalities = tuple(count + increment for count in cardinalities)
    return cardinalities


def repeat_target(state: _ValidationState, value: str, pointer: str) -> tuple[str, str]:
    if value.count(".") != 1:
        fail(state, "LSW303", "repeat.until must be a single producer outcome reference", f"{pointer}/repeat/until", "use <verify-id>.passed")
    producer, suffix = value.split(".")
    if not _ID.fullmatch(producer):
        fail(state, "LSW303", "repeat.until has an invalid producer id", f"{pointer}/repeat/until", "use <verify-id>.passed")
    return producer, suffix
