"""The lockstep recipe profile (pure YAML analysis, no yamlgraph import).

Every bad fixture under fixtures/recipes/bad/ maps to exactly one rule via
MARKERS below, plus one warning-only fixture; the mapping is asserted to be
exhaustive so a new fixture cannot land unmapped.
"""

from pathlib import Path

import pytest
import yaml

from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.profile_check import check_recipe, check_recipe_full

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"
# repo-level example recipes — engine/tests/../../recipes/examples.
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
    # EXTRA: every interrupt must declare idempotent: false.
    "not-idempotent.yaml": "idempotent",
    # EXTRA: loop_exits -> interrupt directly.
    "loop-exit-direct-to-interrupt.yaml": "gated through passthrough",
    # subcall triple rules
    "subcall-no-poll.yaml": "poll",
    "subcall-spawn-not-direct.yaml": "direct conditional successor",
    "subcall-undeclared-state.yaml": "_subcall_status",
    "subcall-bad-runner-name.yaml": "runner name",
    "subcall-infinite-timeout.yaml": "positive number of minutes",
    "subcall-spawn-edge-condition.yaml": "verdict_status equality",
    "subcall-no-prompt.yaml": "prompt",
    # a START -> spawn edge bypasses done()-time policy prediction
    # and fires with an empty evidence channel — a spawn must follow a
    # validator.
    "subcall-start-spawn.yaml": "must not be entered from START",
    # work-interrupt step names must be unique — spawn prediction and
    # scenario_done key on them; a collision reads the wrong validator.
    "duplicate-step.yaml": "duplicate step name",
    # a schema jsonschema itself rejects fails here, not from inside every
    # later scenario_done.
    "invalid-evidence-schema.yaml": "invalid evidence_schema",
    # yamlgraph types `message` as `str | dict`; a bare string is a recipe
    # error, never an AttributeError out of the checker.
    "message-not-a-mapping.yaml": "message must be a mapping",
}


def test_good_recipes_pass():
    assert check_recipe(GOOD / "minimal.yaml") == []
    assert check_recipe(GOOD / "two-steps.yaml") == []


def test_each_bad_fixture_yields_its_violation():
    assert len(MARKERS) == 25
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


SUB = GOOD / "subcall-one-shot.yaml"


def test_good_subcall_recipe_passes():
    # errs == [] also proves the marker exemption: this marker has no
    # task/exit_criterion/checks and no validator pairing — only the
    # third-interrupt-class exemption lets it pass.
    assert check_recipe(SUB) == []


@pytest.mark.skipif(
    not (GOOD / "subcall-fractal.yaml").exists(), reason="fractal fixture absent"
)
def test_child_recipes_dir_kwarg_resolves_fractal_children(tmp_path):
    # Engine.start() profiles a staging copy inside state_dir/runs/ —
    # "beside the recipe" resolves to nothing there. The kwarg must fix it,
    # and its ABSENCE must fail (proves the default is really beside-file).
    staged = tmp_path / "staged.yaml"
    staged.write_bytes((GOOD / "subcall-fractal.yaml").read_bytes())
    assert any("not found" in e for e in check_recipe(staged))
    assert check_recipe(staged, child_recipes_dir=GOOD) == []


def test_example_recipes_pass_profile_and_validate():
    recipes = sorted(EXAMPLES.glob("*.yaml"))
    assert recipes, f"no example recipes found under {EXAMPLES}"
    for recipe in recipes:
        assert check_recipe(recipe) == [], f"{recipe.name}: profile errors: {check_recipe(recipe)}"
        ok, msg = yg.cli_validate(recipe)
        assert ok, f"{recipe.name}: cli_validate failed: {msg}"


def test_reviewed_example_declares_channels_and_pins_the_artifact():
    doc = yaml.safe_load((EXAMPLES / "feature-dev-reviewed.yaml").read_text())
    assert {"_subcall_status", "_subcall_envelope"} <= set(doc["state"])
    markers = [(n.get("message") or {}) for n in doc["nodes"].values()
               if n.get("type") == "interrupt"
               and (n.get("message") or {}).get("step") == "_subcall"]
    assert len(markers) == 1
    assert markers[0]["scenario"] == "review-gate"
    assert markers[0]["artifacts"] == {"review": ".lockstep/review.md"}
    assert "pre-approval" in markers[0]["prompt"]          # the poisoning warning ships in the prompt
    checks = [c for n in doc["nodes"].values() if n.get("type") == "interrupt"
              for c in ((n.get("message") or {}).get("checks") or [])]
    assert any(c.get("type") == "file_matches_hash"
               and c.get("hash_from") == "_subcall_envelope.artifact_hashes.review"
               for c in checks)


def test_invalid_evidence_schema_is_a_recipe_error():
    # A schema jsonschema itself rejects raises SchemaError from every
    # scenario_done on that step, so the recipe must not validate ok.
    errors = check_recipe(BAD / "invalid-evidence-schema.yaml")
    assert any("invalid evidence_schema" in e for e in errors)


def test_child_scenario_name_cannot_walk_out_of_the_recipes_dir(tmp_path):
    # The scenario names the child's pinned snapshot file and its run_id
    # prefix, so a separator in it places child run state outside the state
    # dir. Refused where a recipe error belongs.
    staged = tmp_path / "staged.yaml"
    doc = yaml.safe_load((GOOD / "subcall-fractal.yaml").read_text())
    for node in doc["nodes"].values():
        msg = node.get("message")
        if isinstance(msg, dict) and msg.get("scenario"):
            msg["scenario"] = "../outside/evil"
    staged.write_text(yaml.safe_dump(doc))
    errors = check_recipe(staged, child_recipes_dir=GOOD)
    assert any("scenario name" in e for e in errors)


def _fractal_doc():
    return yaml.safe_load((GOOD / "subcall-one-shot.yaml").read_text())


def test_uncapped_cycle_through_a_subcall_marker_is_refused(tmp_path):
    # The poll-loop exemption belongs to the POLL node, whose termination
    # is the runner timeout. Keyed on the marker alone, any cycle the walk
    # happens to enter at the marker — here a plain passthrough wired back
    # to it — escapes loop_limits/loop_exits and runs forever.
    doc = _fractal_doc()
    doc["nodes"]["fixup"] = {"type": "passthrough", "output": {}}
    edges = [e for e in doc["edges"]
             if not (e["from"] == "review_poll" and e.get("condition", "").endswith("'done'"))]
    edges.append({"from": "review_poll", "to": "fixup",
                  "condition": "_subcall_status == 'done'"})
    edges.append({"from": "fixup", "to": "review_wait"})
    doc["edges"] = edges
    staged = tmp_path / "staged.yaml"
    staged.write_text(yaml.safe_dump(doc))

    errors = check_recipe(staged)
    assert any("fixup" in e and "loop" in e for e in errors), errors


@pytest.mark.parametrize("cap", [None, 0, -1, "lots"])
def test_a_loop_limit_that_caps_nothing_is_refused(tmp_path, cap):
    # Presence is not a cap: the rule reads "every retry loop must be
    # capped", and `null`/`0`/`-1`/`"lots"` all declare a key that limits
    # no execution at all.
    doc = yaml.safe_load((GOOD / "minimal.yaml").read_text())
    doc["loop_limits"] = {"validate_one": cap}
    staged = tmp_path / "staged.yaml"
    staged.write_text(yaml.safe_dump(doc))

    errors = check_recipe(staged)
    assert any("positive integer" in e for e in errors), errors
