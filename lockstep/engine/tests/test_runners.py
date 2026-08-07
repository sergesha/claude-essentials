import os, stat, textwrap
import pytest
from lockstep_mcp.runners import (RunnerError, load_runners, resolve, build_argv, child_env, DEFAULTS)

def _write_runners(tmp_path, exe_path, extra=""):
    (tmp_path / "runners.yaml").write_text(textwrap.dedent(f"""
        runners:
          claude:
            path: {exe_path}
            models: [claude-haiku-4-5]
            timeout_minutes: 5
        budgets:
          max_subcalls_per_run: 3
          max_fractal_depth: 2
        {extra}
    """))

def _fake_exe(tmp_path):
    p = tmp_path / "fake-claude"
    p.write_text("#!/usr/bin/env python3\nprint('{}')\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p

def test_absent_config_is_empty(tmp_path):
    assert load_runners(tmp_path) == {}

def test_node_runner_wins_then_env_default(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    assert spec.name == "claude" and spec.path == str(exe) and spec.timeout_minutes == 5
    spec2 = resolve(tmp_path, None, {"LOCKSTEP_RUNNER": "claude"})
    assert spec2.name == "claude"

def test_unknown_runner_refuses_loudly_no_substitution(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    with pytest.raises(RunnerError) as e:
        resolve(tmp_path, "codex", {"LOCKSTEP_RUNNER": "claude"})
    assert "codex" in str(e.value) and "allowlist" in str(e.value)

def test_no_runner_named_anywhere_refuses(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    with pytest.raises(RunnerError):
        resolve(tmp_path, None, {})

def test_relative_path_is_rejected(tmp_path):
    _write_runners(tmp_path, "claude")            # not absolute -> PATH planting risk
    with pytest.raises(RunnerError) as e:
        resolve(tmp_path, "claude", {})
    assert "absolute" in str(e.value)

def test_non_executable_target_is_rejected(tmp_path):
    p = tmp_path / "not-exec"; p.write_text("x")
    _write_runners(tmp_path, p)
    with pytest.raises(RunnerError) as e:
        resolve(tmp_path, "claude", {})
    assert "executable" in str(e.value)

def test_budgets_default_when_absent(tmp_path):
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(f"runners:\n  claude:\n    path: {exe}\n")
    spec = resolve(tmp_path, "claude", {})
    assert spec.max_fractal_depth == DEFAULTS["max_fractal_depth"]
    assert spec.timeout_minutes == DEFAULTS["timeout_minutes"]

def test_model_must_be_allowlisted(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    argv = build_argv(spec, "do it", "claude-haiku-4-5", None)
    assert argv[0] == str(exe) and "-p" in argv and "--output-format" in argv
    with pytest.raises(RunnerError):
        build_argv(spec, "do it", "gpt-hacker", None)

def test_child_env_allowlist(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    base = {"PATH": "/bin", "SECRET_TOKEN": "leak", "LOCKSTEP_CHILD_NONCE": "old",
            "LOCKSTEP_STATE_DIR": "/should/be/overridden"}
    env = child_env(spec, base, tmp_path, "run-1", "n1")
    assert env["LOCKSTEP_STATE_DIR"] == str(tmp_path)   # preserved-and-pinned: shared index is load-bearing
    assert env["LOCKSTEP_CHILD_RUN"] == "run-1" and env["LOCKSTEP_CHILD_NONCE"] == "n1"
    assert "SECRET_TOKEN" not in env and env["PATH"] == "/bin"

def test_child_env_strips_credentials_for_one_shot(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    env = child_env(spec, {"PATH": "/bin", "LOCKSTEP_CHILD_RUN": "stale", "LOCKSTEP_CHILD_NONCE": "stale"},
                    tmp_path, None, None)
    assert "LOCKSTEP_CHILD_RUN" not in env and "LOCKSTEP_CHILD_NONCE" not in env
