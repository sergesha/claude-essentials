"""Shared leaf validations used by semantic block responsibilities."""

from __future__ import annotations

from ._semantics_contracts import RetryContract
from ._semantics_validation import _ValidationState, fail
from .ir import RetryIR


def handlers(
    state: _ValidationState,
    on_failure: str | None,
    on_error: str | None,
    pointer: str,
) -> None:
    for key, value in (("on_failure", on_failure), ("on_error", on_error)):
        if value not in {None, "escalate"}:
            fail(state, "LSW120", "v1 outcome handlers must be escalate", f"{pointer}/{key}", "use escalate or omit the handler")


def retry(
    state: _ValidationState, retry_ir: RetryIR | None, pointer: str
) -> RetryContract | None:
    if retry_ir is None:
        return None
    if retry_ir.limit < 1 or retry_ir.exhausted != "escalate":
        fail(state, "LSW303", "retry must have a positive total-execution limit and exhausted: escalate", pointer, "use a positive limit and exhausted: escalate")
    return RetryContract(retry_ir.limit, retry_ir.exhausted, retry_ir.limit)
