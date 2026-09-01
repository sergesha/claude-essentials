"""Fail-closed policy and session decision core for PreToolUse."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.status import ScenarioStatus


def _matching_policy(
    policy_dir: Path,
    cwd: str,
    project_matches: Callable[[str, str], bool],
) -> dict | None:
    matching: dict | None = None
    matching_depth = -1
    for path in sorted(policy_dir.glob("*.yaml")):
        document = yaml.safe_load(path.read_text()) or {}
        project = document.get("project")
        if project and project_matches(project, cwd):
            depth = len(Path(project).resolve().parts)
            if depth > matching_depth:
                matching = document
                matching_depth = depth
    return matching


def _policy_binding(policy: dict) -> tuple[str, str] | None:
    recipe = policy.get("recipe")
    recipe_digest = policy.get("recipe_digest")
    if (
        not isinstance(recipe, str)
        or not recipe
        or not isinstance(recipe_digest, str)
        or len(recipe_digest) != 64
    ):
        return None
    return recipe, recipe_digest


def _matching_candidates(
    active: Iterable[tuple[RunBinding, ScenarioStatus]],
    *,
    project: object,
    recipe_digest: str,
) -> tuple[tuple[RunBinding, ScenarioStatus], ...]:
    return tuple(
        (binding, status)
        for binding, status in active
        if status.status == "awaiting"
        and Path(binding.project_identity).resolve() == Path(project).resolve()
        and binding.recipe_digest == recipe_digest
    )


def _session_decision(
    state_dir: Path,
    candidates: tuple[tuple[RunBinding, ScenarioStatus], ...],
    *,
    session_id: object,
    stale_minutes: float,
    recipe: str,
) -> str | None:
    if not isinstance(session_id, str) or not session_id:
        return (
            "lockstep policy: hook input carried no session_id — run "
            "ownership cannot be established; failing closed"
        )
    for binding, _status in candidates:
        if sessions.refresh_if_owner(
            state_dir,
            binding.public_run_id,
            session_id,
            stale_minutes,
        ):
            return None
    binding, _status = candidates[0]
    run_id = binding.public_run_id
    if sessions.is_live(sessions.read_binding(state_dir, run_id), stale_minutes):
        return (
            f"lockstep policy: run {run_id} of recipe {recipe} is being driven "
            "by another live session — writes here belong to that session. If it "
            f"is truly gone it becomes stale after {stale_minutes:g}m; "
            "scenario_start a fresh run"
        )
    return (
        f"lockstep policy: run {run_id} of recipe {recipe} has no live driving "
        "session — scenario_start a fresh run"
    )


def decide_pretool(
    stdin_json: dict,
    state_dir: Path,
    *,
    policy_dir_for: Callable[[Path], Path],
    project_matches: Callable[[str, str], bool],
    active_native: Callable[[Path], Iterable[tuple[RunBinding, ScenarioStatus]]],
    stale_minutes_for: Callable[[], float],
) -> str | None:
    cwd = stdin_json.get("cwd") or os.getcwd()
    policy_dir = policy_dir_for(state_dir)
    if not policy_dir.exists():
        return None
    policy = _matching_policy(policy_dir, cwd, project_matches)
    if policy is None:
        return None
    binding = _policy_binding(policy)
    if binding is None:
        return "lockstep policy: configured recipe binding is invalid"
    recipe, recipe_digest = binding
    candidates = _matching_candidates(
        active_native(state_dir),
        project=policy["project"],
        recipe_digest=recipe_digest,
    )
    if not candidates:
        return f"lockstep policy: start recipe {recipe} via scenario_start first"
    return _session_decision(
        state_dir,
        candidates,
        session_id=stdin_json.get("session_id"),
        stale_minutes=stale_minutes_for(),
        recipe=recipe,
    )
