"""Check registry + run_checks — Task 1 stub.

Replaced in Task 2 with the real deterministic check registry
(file_exists, cmd_ok, junit_gate, baseline checks, ...). For this task
run_checks is exactly the in-graph republish path the real engine will use
after decision 16: the graph node never executes checks itself — it reads
the verdict the engine already embedded in the resume payload's evidence
and republishes it flat. Per the Task-1 stub spec, default (no embedded
verdict) is "fail"; Task 2 tightens the no-embedded-verdict case to
"error" (anti-forgery) as part of the real execute=True/False contract.
"""

from typing import Any


def run_checks(state: dict[str, Any]) -> dict[str, Any]:
    """In-graph republish path (spike stub — see module docstring)."""
    evidence = state.get("evidence") or {}
    status = evidence.get("_verdict_status", "fail")
    return {"verdict_status": status, "verdict_reasons": []}
