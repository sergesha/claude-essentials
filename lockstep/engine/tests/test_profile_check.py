"""Task 3: the lockstep recipe profile (pure YAML analysis, no yamlgraph
import). Bad fixtures: the plan's original 12, plus 2 EXTRA fixtures for
rules the spike findings introduced/reshaped after the plan's fixture list
was written (spike finding 4 - idempotent; spike finding 3's gate-shape
case) - 14 bad fixtures total, plus one warning fixture.
"""

from pathlib import Path

from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.profile_check import check_recipe, check_recipe_full

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"
# Task 8: repo-level example recipes — engine/tests/../../recipes/examples.
EXAMPLES = Path(__file__).resolve().parents[2] / "recipes" / "examples"

# fixture stem -> substring that MUST appear in check_recipe(fixture)
MARKERS = {
    "llm-node.yaml": "forbidden node type",
    "no-validator.yaml": "no validator",
    "bypass-edge.yaml": "bypass",
    "uncapped-loop.yaml": "loop_limits",
    "no-escalate-exit.yaml": "loop_exits must target",
    "unmarked-escalate.yaml": "escalate marker",
    "has-checkpointer.yaml": "checkpointer",
    "brief-missing-fields.yaml": "exit_criterion",
    "command-from.yaml": "command_from",
    "placeholder-in-checks.yaml": "placeholder",
    "unannotated-path.yaml": "project-path annotation",
    "baseline-check-no-globs.yaml": "baseline_globs",
    # EXTRA (spike finding 4): every interrupt must declare idempotent: false.
    "not-idempotent.yaml": "idempotent",
    # EXTRA (spike finding 3 gate shape): loop_exits -> interrupt directly.
    "loop-exit-direct-to-interrupt.yaml": "gated through passthrough",
}


def test_good_recipes_pass():
    assert check_recipe(GOOD / "minimal.yaml") == []
    assert check_recipe(GOOD / "two-steps.yaml") == []


def test_each_bad_fixture_yields_its_violation():
    assert len(MARKERS) == 14
    for fixture, marker in MARKERS.items():
        errors = check_recipe(BAD / fixture)
        assert errors, f"{fixture}: expected errors, got none"
        assert any(marker in e for e in errors), (
            f"{fixture}: expected an error containing {marker!r}, got {errors}"
        )


def test_local_tools_py_is_warning_not_error():
    errors, warnings = check_recipe_full(BAD / "local-tools-py.yaml")
    assert errors == []
    assert warnings
    assert any("tools.py" in w for w in warnings)


def test_bad_fixture_count_matches_marker_map():
    bad_fixtures = {p.name for p in BAD.glob("*.yaml")}
    # every fixture in BAD/ is either a marker-mapped error case or the
    # dedicated warning fixture - nothing stray, nothing missing.
    assert bad_fixtures == set(MARKERS) | {"local-tools-py.yaml"}


def test_example_recipes_pass_profile_and_validate():
    recipes = sorted(EXAMPLES.glob("*.yaml"))
    assert recipes, f"no example recipes found under {EXAMPLES}"
    for recipe in recipes:
        assert check_recipe(recipe) == [], f"{recipe.name}: profile errors: {check_recipe(recipe)}"
        ok, msg = yg.cli_validate(recipe)
        assert ok, f"{recipe.name}: cli_validate failed: {msg}"
