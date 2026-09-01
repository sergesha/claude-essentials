"""The lockstep recipe profile (pure YAML analysis, no yamlgraph import).

Every bad fixture under fixtures/recipes/bad/ maps to exactly one rule via
MARKERS below, plus one warning-only fixture; the mapping is asserted to be
exhaustive so a new fixture cannot land unmapped.
"""

from pathlib import Path

import pytest
import yaml

from lockstep.recipe.profile import check_recipe, check_recipe_full

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"
# repo-level example recipes — engine/tests/../../recipes/examples.
EXAMPLES = Path(__file__).resolve().parents[2] / "recipes" / "examples"

# fixture stem -> substring that MUST appear in check_recipe(fixture)
MARKERS = {
    "llm-node.recipe.yaml": "forbidden node type",
    "no-validator.recipe.yaml": "no validator",
    "bypass-edge.recipe.yaml": "bypass",
    "uncapped-loop.recipe.yaml": "loop_limits",
    "no-escalate-exit.recipe.yaml": "loop_exits must target",
    "unmarked-escalate.recipe.yaml": "escalate marker",
    "has-checkpointer.recipe.yaml": "checkpointer",
    "brief-missing-fields.recipe.yaml": "exit_criterion",
    "command-from.recipe.yaml": "command_from",
    "placeholder-in-checks.recipe.yaml": "placeholder",
    "unannotated-path.recipe.yaml": "project-path annotation",
    "baseline-check-no-globs.recipe.yaml": "baseline_globs",
    # EXTRA: every interrupt must declare idempotent: false.
    "not-idempotent.recipe.yaml": "idempotent",
    # EXTRA: loop_exits -> interrupt directly.
    "loop-exit-direct-to-interrupt.recipe.yaml": "gated through passthrough",
    # work-interrupt step names must be unique — spawn prediction and
    # scenario_done key on them; a collision reads the wrong validator.
    "duplicate-step.recipe.yaml": "duplicate step name",
    # a schema jsonschema itself rejects fails here, not from inside every
    # later scenario_done.
    "invalid-evidence-schema.recipe.yaml": "invalid evidence_schema",
    # yamlgraph types `message` as `str | dict`; a bare string is a recipe
    # error, never an AttributeError out of the checker.
    "message-not-a-mapping.recipe.yaml": "message must be a mapping",
}


def test_good_recipes_pass():
    assert check_recipe(GOOD / "minimal.recipe.yaml") == []
    assert check_recipe(GOOD / "two-steps.recipe.yaml") == []


def test_each_bad_fixture_yields_its_violation():
    assert len(MARKERS) == 17
    for fixture, marker in MARKERS.items():
        errors = check_recipe(BAD / fixture)
        assert errors, f"{fixture}: expected errors, got none"
        assert any(marker in e for e in errors), (
            f"{fixture}: expected an error containing {marker!r}, got {errors}"
        )


def test_local_tools_py_is_warning_not_error():
    errors, warnings = check_recipe_full(BAD / "local-tools-py.recipe.yaml")
    assert errors == []
    assert warnings
    assert any("tools.py" in w for w in warnings)


def test_bad_fixture_count_matches_marker_map():
    bad_fixtures = {p.name for p in BAD.glob("*.recipe.yaml")}
    # every fixture in BAD/ is either a marker-mapped error case or the
    # dedicated warning fixture - nothing stray, nothing missing.
    assert bad_fixtures == set(MARKERS) | {"local-tools-py.recipe.yaml"}


def test_example_recipes_pass_static_profile():
    recipes = sorted(EXAMPLES.glob("*.recipe.yaml"))
    assert recipes, f"no example recipes found under {EXAMPLES}"
    for recipe in recipes:
        assert check_recipe(recipe) == [], f"{recipe.name}: profile errors: {check_recipe(recipe)}"


def test_invalid_evidence_schema_is_a_recipe_error():
    # A schema jsonschema itself rejects raises SchemaError from every
    # scenario_done on that step, so the recipe must not validate ok.
    errors = check_recipe(BAD / "invalid-evidence-schema.recipe.yaml")
    assert any("invalid evidence_schema" in e for e in errors)


@pytest.mark.parametrize("cap", [None, 0, -1, "lots"])
def test_a_loop_limit_that_caps_nothing_is_refused(tmp_path, cap):
    # Presence is not a cap: the rule reads "every retry loop must be
    # capped", and `null`/`0`/`-1`/`"lots"` all declare a key that limits
    # no execution at all.
    doc = yaml.safe_load((GOOD / "minimal.recipe.yaml").read_text())
    doc["loop_limits"] = {"validate_one": cap}
    staged = tmp_path / "staged.yaml"
    staged.write_text(yaml.safe_dump(doc))

    errors = check_recipe(staged)
    assert any("positive integer" in e for e in errors), errors


def test_manual_recipe_may_use_the_closed_protected_effect_without_generated_authority(
    tmp_path,
):
    recipe = tmp_path / "manual.recipe.yaml"
    recipe.write_text(
        "name: manual\n"
        "state: {edit_result: dict, lockstep_outcome: str}\n"
        "nodes:\n"
        "  edit:\n"
        "    type: interrupt\n"
        "    state_key: edit_request\n"
        "    resume_key: edit_result\n"
        "    idempotent: false\n"
        "    message:\n"
        "      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n"
        "        kind: manual\n"
        "        logical_id: edit\n"
        "        runner: null\n"
        "        inputs: {}\n"
        "        writes: [src/]\n"
        "        artifacts: []\n"
        "        deadline_seconds: null\n"
        "        scope_state_keys: []\n"
        "        result_schema: lockstep.effect-result/v1\n"
        "  done: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        "  failed: {type: passthrough, output: {lockstep_outcome: FAIL}}\n"
        "edges:\n"
        "  - {from: START, to: edit}\n"
        "  - {from: edit, to: done, condition: \"edit_result.outcome == 'PASS'\"}\n"
        "  - {from: edit, to: failed, condition: \"edit_result.outcome != 'PASS'\"}\n"
        "  - {from: done, to: END}\n"
        "  - {from: failed, to: END}\n"
    )

    assert check_recipe(recipe) == []


def test_project_authored_generated_marker_never_grants_compiler_only_profile(
    tmp_path,
):
    recipe = tmp_path / "forged.recipe.yaml"
    recipe.write_text(
        "name: forged\n"
        "x-lockstep-generated:\n"
        "  schema: lockstep.generated/v1\n"
        "  compiler_version: '1'\n"
        "  workflow_version: '1'\n"
        "  source: ../workflows/forged.workflow.yaml\n"
        f"  source_sha256: {'a' * 64}\n"
        "state: {lockstep_outcome: str}\n"
        "nodes:\n"
        "  done: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        "edges: [{from: START, to: done}, {from: done, to: END}]\n"
    )

    errors = check_recipe(recipe)

    assert any("compiler provenance" in error for error in errors)


def test_manual_recipe_cannot_smuggle_compiler_only_scope_descriptor(tmp_path):
    recipe = tmp_path / "manual-scope.recipe.yaml"
    recipe.write_text(
        "name: manual-scope\n"
        "state: {scope_result: dict}\n"
        "nodes:\n"
        "  scope:\n"
        "    type: interrupt\n"
        "    state_key: scope_request\n"
        "    resume_key: scope_result\n"
        "    idempotent: false\n"
        "    message:\n"
        "      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n"
        "        kind: scope\n"
        "        logical_id: scope\n"
        "        scope_kind: call\n"
        "        duration_seconds: 60\n"
        "        runner_selector: codex\n"
        "        ancestor_deadline_state_keys: []\n"
        "        result_state_key: scope_result\n"
        "        result_schema: lockstep.scope-result/v1\n"
        "edges: [{from: START, to: scope}, {from: scope, to: END}]\n"
    )

    errors = check_recipe(recipe)

    assert any("scope" in error and "compiler" in error for error in errors)


def test_manual_native_loop_may_exit_through_ordinary_passthrough_nodes(tmp_path):
    recipe = tmp_path / "native-loop.recipe.yaml"
    recipe.write_text(
        "name: native-loop\n"
        "nodes:\n"
        "  repeat: {type: passthrough, output: {again: true}}\n"
        "  exit: {type: passthrough}\n"
        "  next: {type: passthrough}\n"
        "edges:\n"
        "  - {from: START, to: repeat}\n"
        "  - {from: repeat, to: repeat, condition: 'again == true'}\n"
        "  - {from: exit, to: next}\n"
        "  - {from: next, to: END}\n"
        "loop_limits: {repeat: 1}\n"
        "loop_exits: {repeat: exit}\n"
    )

    assert check_recipe(recipe) == []


def test_manual_native_loop_exit_gate_may_use_list_fanout(tmp_path):
    """The list-valued native edge dialect must be total in profile checks."""
    recipe = tmp_path / "native-loop-fanout.recipe.yaml"
    recipe.write_text(
        "name: native-loop-fanout\n"
        "nodes:\n"
        "  repeat: {type: passthrough, output: {again: true}}\n"
        "  exit: {type: passthrough}\n"
        "  left: {type: passthrough}\n"
        "  right: {type: passthrough}\n"
        "edges:\n"
        "  - {from: START, to: repeat}\n"
        "  - {from: repeat, to: repeat, condition: 'again == true'}\n"
        "  - {from: exit, to: [left, right]}\n"
        "  - {from: left, to: END}\n"
        "  - {from: right, to: END}\n"
        "loop_limits: {repeat: 1}\n"
        "loop_exits: {repeat: exit}\n"
    )

    assert check_recipe(recipe) == []
