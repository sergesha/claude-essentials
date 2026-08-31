"""Identity and eligibility decisions for PostToolUse session binding."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lockstep.runtime.status import ScenarioStatus


def _posttool_identity(
    stdin_json: dict,
    *,
    known_tool: Callable[[str], bool],
    posttool_run_id: Callable[[object, object, str], str | None],
    find_marked_run_id: Callable[[object], str | None],
) -> tuple[str, str] | None:
    tool_name = str(stdin_json.get("tool_name") or "")
    if not tool_name.startswith("mcp__") or not tool_name.endswith(
        "__scenario_start"
    ):
        return None
    tool_input = stdin_json.get("tool_input")
    if isinstance(tool_input, dict):
        input_run_id = tool_input.get("run_id")
        if isinstance(input_run_id, str) and input_run_id:
            return None
    tool_response = stdin_json.get("tool_response")
    if isinstance(tool_response, dict) and tool_response.get("isError") is True:
        return None
    if known_tool(tool_name):
        run_id = posttool_run_id(
            tool_input,
            tool_response,
            tool_name,
        )
    else:
        run_id = find_marked_run_id(tool_response)
    session_id = stdin_json.get("session_id")
    if not run_id or not isinstance(session_id, str) or not session_id:
        return None
    return run_id, session_id


def _worker_awaiting(
    projected: Mapping[str, ScenarioStatus], run_id: str
) -> bool:
    status = projected.get(run_id)
    return (
        status is not None
        and status.status == "awaiting"
        and status.owner == "worker"
    )
