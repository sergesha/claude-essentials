"""Declared manual artifacts gate completion through the public CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("contents", "diagnostic"),
    ((None, "audit.md"), ("# Rules\nKeep valid orders.\n", "Findings")),
    ids=("missing-file", "missing-heading"),
)
def test_manual_artifact_rejection_can_be_repaired_on_the_same_step(
    tmp_path: Path, contents: str | None, diagnostic: str
) -> None:
    # Skipping the declared artifact checks must let the premature done through
    # and fail this test. All observations use separate public CLI processes.
    project = tmp_path / "project"
    source = project / ".lockstep/workflows/audit.workflow.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        "workflow_version: '1'\n"
        "name: audit\n"
        "description: Validate a declared manual artifact.\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - step: audit\n"
        "    task: Write the order audit.\n"
        "    exit: The audit contains rules and findings.\n"
        "    writes: [audit.md]\n"
        "    artifact:\n"
        "      handle: audit\n"
        "      path: audit.md\n"
        "      markdown: {sections: [Rules, Findings]}\n"
        "    retry: {limit: 1, exhausted: escalate}\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "LOCKSTEP_STATE_DIR": str(tmp_path / "owner-state"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "lockstep", *args],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    compiled = cli("recipe", "compile", "audit")
    assert compiled.returncode == 0, compiled.stderr
    started = cli("scenario", "start", "audit", "--session-id", "audit-worker")
    assert started.returncode == 0, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    artifact = project / "audit.md"
    if contents is not None:
        artifact.write_text(contents, encoding="utf-8")

    rejected = cli(
        "scenario", "done", run_id, "audit",
        "--evidence", "{}", "--session-id", "audit-worker",
    )
    assert rejected.returncode != 0, rejected.stdout
    assert diagnostic in rejected.stderr
    pending = cli("scenario", "status", run_id)
    assert pending.returncode == 0, pending.stderr
    status = json.loads(pending.stdout)
    assert status["run_id"] == run_id
    assert status["status"] == "awaiting"
    assert status["step"] == "audit"
    if contents is None:
        assert not artifact.exists()
    else:
        assert artifact.read_text(encoding="utf-8") == contents

    artifact.write_text("# Rules\nKeep valid orders.\n# Findings\nNo invalid orders.\n", encoding="utf-8")
    repaired = cli(
        "scenario", "done", run_id, "audit",
        "--evidence", "{}", "--session-id", "audit-worker",
    )
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout)["status"] == "completed"
    final = cli("scenario", "status", run_id)
    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["status"] == "completed"
