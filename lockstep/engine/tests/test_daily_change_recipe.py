"""The shipped daily-change recipe must use the native direct-child path."""

from __future__ import annotations

from pathlib import Path

import yaml

from lockstep.recipe.profile import check_recipe_full
from lockstep.recipe import yamlgraph_adapter as yg


def test_daily_change_review_recipe_composes_the_reviewer_as_a_direct_child() -> None:
    """Catches shipping the flagship review flow with the removed child scheduler."""
    recipe = (
        Path(__file__).parents[2]
        / "recipes"
        / "examples"
        / "daily-change-reviewed.recipe.yaml"
    )
    document = yaml.safe_load(recipe.read_text())
    children = [
        node
        for node in document["nodes"].values()
        if node.get("type") == "subgraph"
    ]

    assert len(children) == 1
    assert children[0]["mode"] == "direct"
    assert children[0]["graph"] == "daily-review-gate.recipe.yaml"
    assert not any(name.startswith("_subcall") for name in document["state"])
    assert {"step_plan", "step_tests", "step_implement", "step_verify", "review", "step_accept"} <= set(document["nodes"])
    edges = {(edge["from"], edge["to"]) for edge in document["edges"]}
    assert ("validate_plan", "step_tests") in edges
    assert ("validate_tests", "step_implement") in edges
    assert ("validate_implement", "step_verify") in edges
    assert ("validate_verify", "review") in edges
    assert ("review", "step_accept") in edges
    assert ("step_accept", "validate_accept") in edges
    assert set(document["loop_limits"]) == {
        "validate_plan", "validate_tests", "validate_implement",
        "validate_verify", "validate_accept",
    }


def test_daily_change_review_recipe_recursively_profiles_and_compiles_natively() -> None:
    recipe = (
        Path(__file__).parents[2]
        / "recipes"
        / "examples"
        / "daily-change-reviewed.recipe.yaml"
    )
    errors, _warnings = check_recipe_full(recipe)
    assert errors == []

    app = yg._open_native_path(recipe)  # noqa: SLF001 - shipped native oracle
    started = app.invoke(
        {"brief": {"task": "daily change"}},
        thread_id="daily-change-reviewed",
    )
    app.close()
    assert len(started.pending) == 1
    assert started.pending[0].value["step"] == "plan"
