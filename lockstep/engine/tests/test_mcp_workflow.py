"""The agent can discover and complete its task over the actual MCP transport."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize(
    ("template", "expected_names"),
    [
        ("reviewed-change", ["demo", "demo-review"]),
        ("parallel-review", ["demo", "demo-architecture-review", "demo-security-review"]),
    ],
)
def test_installed_template_recipes_are_discoverable_over_mcp(
    tmp_path: Path, template: str, expected_names: list[str],
):
    project = tmp_path / "project"
    project.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("LOCKSTEP_")}
    env["LOCKSTEP_STATE_DIR"] = str(tmp_path / "state")
    executable = str(Path(sys.executable).with_name("lockstep"))
    installed = subprocess.run(
        [executable, "template", "init", template, "demo"], cwd=project, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    async def exercise():
        parameters = StdioServerParameters(
            command=executable, args=["serve"], cwd=project, env=env,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                result = await client.call_tool("list_recipes", {})
                if not isinstance(result, dict):
                    result = result.model_dump()
                assert not result.get("isError", result.get("is_error")), result
                assert [item["text"] for item in result["content"]] == expected_names

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


@pytest.mark.parametrize(
    ("restore_source", "expected_status"),
    [(True, "completed"), (False, "escalated")],
)
def test_mcp_worker_reads_captured_task_and_observes_result_after_restart(
    tmp_path: Path, restore_source: bool, expected_status: str,
):
    project = tmp_path / "project"
    workflows = project / ".lockstep" / "workflows"
    workflows.mkdir(parents=True)
    source = workflows / "review.workflow.yaml"
    original = """workflow_version: '1'
name: review
description: A review report
protect: ['**']
flow:
  - step: report
    task: Write the review findings.
    exit: The report contains findings.
    writes: [review.md]
    artifact:
      handle: review
      path: review.md
      markdown: {sections: [Findings]}
"""
    source.write_text(original)
    env = {key: value for key, value in os.environ.items() if not key.startswith("LOCKSTEP_")}
    env["LOCKSTEP_STATE_DIR"] = str(tmp_path / "state")
    executable = str(Path(sys.executable).with_name("lockstep"))
    compiled = subprocess.run(
        [executable, "recipe", "compile", "review"], cwd=project, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr

    @asynccontextmanager
    async def connected():
        parameters = StdioServerParameters(
            command=executable, args=["serve"], cwd=project, env=env,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client

    async def call(client, name, arguments):
        result = await client.call_tool(name, arguments, meta={"session_id": "worker"})
        if not isinstance(result, dict):
            result = result.model_dump()
        assert not result.get("isError"), result
        values = [json.loads(item["text"]) for item in result["content"] if item["type"] == "text"]
        return (values if name == "scenario_events" else values[0]), result

    async def exercise():
        async with connected() as client:
            started, response = await call(client, "scenario_start", {"recipe": "review"})
            run_id = started["run_id"]
            subprocess.run(
                [executable, "hook-posttool"], cwd=project, env=env,
                input=json.dumps({
                    "session_id": "worker", "cwd": str(project),
                    "tool_name": "mcp__lockstep__scenario_start",
                    "tool_input": {"recipe": "review"}, "tool_response": response,
                }),
                check=True, capture_output=True, text=True, timeout=20,
            )

        source.write_text(original.replace("Write the review findings.", "Unadmitted replacement."))
        async with connected() as client:
            status, _ = await call(client, "scenario_status", {"run_id": run_id})
            assert status["task"] == "Write the review findings."
            assert status["exit_criterion"] == "The report contains findings."
            assert status["evidence_schema"] == {}
            assert status["writes"] == ["review.md"]
            assert status["artifact_contract"]["markdown"]["sections"] == ["Findings"]
            if restore_source:
                source.write_text(original)
            report = status["artifact_contract"]["path"]
            (project / report).write_text("# Findings\nNo blocking findings.\n")
            completed, _ = await call(client, "scenario_done", {
                "run_id": run_id, "step": status["step"], "evidence": {"path": report},
            })
            assert completed["status"] == expected_status

        async with connected() as client:
            status, _ = await call(client, "scenario_status", {"run_id": run_id})
            assert status["status"] == expected_status
            assert "task" not in status
            events, _ = await call(client, "scenario_events", {"run_id": run_id})
            manual_events = [
                event for event in events
                if event.get("effect_kind") == "manual"
            ]
            assert len(manual_events) == 1
            if restore_source:
                assert "fixed_error_code" not in manual_events[0]
            else:
                assert manual_events[0]["fixed_error_code"] == "manifest_invalid"

    asyncio.run(asyncio.wait_for(exercise(), timeout=60))
