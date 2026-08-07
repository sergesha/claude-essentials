"""Task 6 base: cli.py verb routing. `serve` is the default verb and runs
the FastMCP app; `hook-stop`/`hook-session-start`/`hook-pretool`/`policy`/
`doctor` are Task-7 stubs here (exit 0, no side effects). This test asserts
DISPATCH — the right handler is called for each verb — not a closed verb
set (Task 7 extends `policy`/`doctor` with subcommands)."""

from __future__ import annotations

import lockstep_mcp.cli as cli


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


def test_stub_verbs_exit_zero_without_side_effects():
    for verb in ["hook-stop", "hook-session-start", "hook-pretool", "policy", "doctor"]:
        assert cli.main([verb]) == 0
