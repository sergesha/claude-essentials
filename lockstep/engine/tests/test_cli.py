"""Task 6 base: cli.py verb routing. `serve` is the default verb and runs
the FastMCP app; `hook-stop`/`hook-session-start`/`hook-pretool`/
`hook-posttool`/`policy` are no-ops with nothing configured. `doctor`'s exit code now reflects
health (m8: missing dirs -> 1). This test asserts
DISPATCH — the right handler is called for each verb — not a closed verb
set.

m6: every verb here that touches `_state_dir()` MUST monkeypatch
`LOCKSTEP_STATE_DIR` to a tmp path — without it, verbs resolve against the
developer's real `~/.lockstep`. `LOCKSTEP_RECIPES` likewise, for
`doctor`."""

from __future__ import annotations

import lockstep_mcp.cli as cli
from lockstep_mcp import __version__


def test_default_verb_dispatches_to_serve(monkeypatch):
    calls = []
    monkeypatch.setitem(cli._HANDLERS, "serve", lambda args: calls.append("serve") or 0)

    assert cli.main([]) == 0
    assert calls == ["serve"]


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
