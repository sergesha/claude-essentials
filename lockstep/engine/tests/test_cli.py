"""cli.py verb routing. Explicit `serve` runs the FastMCP app;
`hook-stop`/`hook-session-start`/`hook-pretool`/
`hook-posttool`/`policy` are no-ops with nothing configured. `doctor`'s
exit code reflects health (missing dirs -> 1). This test asserts
DISPATCH — the right handler is called for each verb — not a closed verb
set.

m6: every verb here that touches `_state_dir()` MUST monkeypatch
`LOCKSTEP_STATE_DIR` to a tmp path — without it, verbs resolve against the
developer's real `~/.lockstep`. `LOCKSTEP_RECIPES` likewise, for
`doctor`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lockstep import __version__, cli
from lockstep.recipe.loader import RecipeLoader

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def test_no_verb_prints_argparse_usage_and_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exit_status:
        cli.main([])

    assert exit_status.value.code == 2
    assert "serve" in capsys.readouterr().err


def test_serve_verb_dispatches_explicitly(monkeypatch):
    calls = []
    monkeypatch.setitem(cli._HANDLERS, "serve", lambda args: calls.append("serve") or 0)

    assert cli.main(["serve"]) == 0
    assert calls == ["serve"]


def test_stub_verbs_exit_zero_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(tmp_path / "recipes"))
    for verb in ["hook-stop", "hook-session-start", "hook-pretool", "hook-posttool", "policy"]:
        assert cli.main([verb]) == 0


def test_policy_require_cli_uses_configured_recipe_root_and_exact_digest(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    child = recipes / "child_interrupt.recipe.yaml"
    child.write_bytes(
        (FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))

    args = [
        "policy",
        "require",
        "--project",
        str(project),
        "--recipe",
        "native-parent-direct",
    ]
    assert cli.main(args) == 0
    policy_path = next((state / "policy.d").glob("*.yaml"))
    first = yaml.safe_load(policy_path.read_text())
    assert first["recipe_digest"] == RecipeLoader(recipes).resolve(
        "native-parent-direct"
    ).definition_sha256

    child.write_text(child.read_text() + "\ndescription: changed child\n")
    assert cli.main(args) == 0
    second = yaml.safe_load(policy_path.read_text())
    assert second["recipe_digest"] != first["recipe_digest"]
    assert second["recipe_digest"] == RecipeLoader(recipes).resolve(
        "native-parent-direct"
    ).definition_sha256


def test_version_flag_prints_version_and_exits_zero(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_doctor_exit_code_reflects_health(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(tmp_path / "recipes"))

    assert cli.main(["doctor"]) == 1  # neither dir exists yet -> issues found

    (tmp_path / "state").mkdir()
    (tmp_path / "recipes").mkdir()
    assert cli.main(["doctor"]) == 0
