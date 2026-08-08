import stat
import textwrap
from pathlib import Path

import pytest

from lockstep_mcp.runners import (
    DEFAULTS,
    RunnerError,
    build_argv,
    child_env,
    load_runners,
    resolve,
)


def _write_runners(tmp_path, exe_path):
    (tmp_path / "runners.yaml").write_text(textwrap.dedent(f"""
        runners:
          claude:
            path: {exe_path}
            models: [claude-haiku-4-5]
            timeout_minutes: 5
        budgets:
          max_subcalls_per_run: 3
          max_fractal_depth: 2
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
    (tmp_path / "runners.yaml").write_text(
        f"runners:\n  claude:\n    path: {exe}\n    models: [claude-haiku-4-5]\n"
    )
    spec = resolve(tmp_path, "claude", {})
    assert spec.max_fractal_depth == DEFAULTS["max_fractal_depth"]
    assert spec.max_subcalls_per_run == DEFAULTS["max_subcalls_per_run"]
    assert spec.timeout_minutes == DEFAULTS["timeout_minutes"]


# ---------------------------------------------------------------------------
# budgets honoured per-runner, unknown runner keys rejected
# ---------------------------------------------------------------------------


def test_per_runner_budget_overrides_all_three(tmp_path):
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(textwrap.dedent(f"""
        runners:
          claude:
            path: {exe}
            models: [claude-haiku-4-5]
            timeout_minutes: 7
            max_subcalls_per_run: 9
            max_fractal_depth: 4
          other:
            path: {exe}
            models: [claude-haiku-4-5]
        budgets:
          timeout_minutes: 11
          max_subcalls_per_run: 12
          max_fractal_depth: 13
    """))
    spec = resolve(tmp_path, "claude", {})
    assert (spec.timeout_minutes, spec.max_subcalls_per_run, spec.max_fractal_depth) == (7, 9, 4)
    other = resolve(tmp_path, "other", {})
    assert (other.timeout_minutes, other.max_subcalls_per_run, other.max_fractal_depth) == (11, 12, 13)


def test_unknown_runner_key_is_rejected(tmp_path):
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(textwrap.dedent(f"""
        runners:
          claude:
            path: {exe}
            models: [claude-haiku-4-5]
            max_subcall_per_run: 3
    """))  # note the typo: would otherwise parse clean and silently default
    with pytest.raises(RunnerError) as e:
        load_runners(tmp_path)
    assert "max_subcall_per_run" in str(e.value)


def test_non_numeric_budget_is_a_runner_error(tmp_path):
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(
        f"runners:\n  claude:\n    path: {exe}\n    models: [claude-haiku-4-5]\n"
        "    timeout_minutes: lots\n"
    )
    with pytest.raises(RunnerError):
        load_runners(tmp_path)


# ---------------------------------------------------------------------------
# argv is byte-exact; the prompt sits LAST behind a `--` terminator
# ---------------------------------------------------------------------------


def test_argv_exact_hostile_prompt_stays_behind_terminator(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    hostile = "--dangerously-skip-permissions"
    argv = build_argv(spec, hostile, "claude-haiku-4-5", None)
    assert argv == [str(exe), "-p", "--output-format", "json",
                    "--model", "claude-haiku-4-5", "--", hostile]


def test_argv_exact_with_resume(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    argv = build_argv(spec, "do it", "claude-haiku-4-5", "sess-1")
    assert argv == [str(exe), "-p", "--output-format", "json",
                    "--model", "claude-haiku-4-5", "--resume", "sess-1", "--", "do it"]


# ---------------------------------------------------------------------------
# model gate never fails open
# ---------------------------------------------------------------------------


def test_missing_models_allowlist_refuses_at_resolve(tmp_path):
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(f"runners:\n  claude:\n    path: {exe}\n")
    with pytest.raises(RunnerError) as e:
        resolve(tmp_path, "claude", {})
    assert "models" in str(e.value)


def test_model_none_pins_first_allowlisted_and_always_emits_flag(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    argv = build_argv(spec, "do it", None, None)
    assert argv == [str(exe), "-p", "--output-format", "json",
                    "--model", "claude-haiku-4-5", "--", "do it"]


def test_model_must_be_allowlisted(tmp_path):
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    with pytest.raises(RunnerError):
        build_argv(spec, "do it", "gpt-hacker", None)


# ---------------------------------------------------------------------------
# re-verify the binary adjacent to spawn
# ---------------------------------------------------------------------------


def test_verified_path_revalidates_immediately_before_use(tmp_path):
    from lockstep_mcp.runners import verified_path

    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    spec = resolve(tmp_path, "claude", {})
    assert verified_path(spec) == str(exe)
    exe.unlink()                                  # swapped/removed after resolve
    with pytest.raises(RunnerError):
        verified_path(spec)


# ---------------------------------------------------------------------------
# the trust anchor must not live inside the agent-writable project tree
# ---------------------------------------------------------------------------


def test_state_dir_inside_project_is_rejected(tmp_path):
    from lockstep_mcp.runners import assert_state_dir_sane

    project = tmp_path / "proj"
    (project / ".lockstep").mkdir(parents=True)
    with pytest.raises(RunnerError):
        assert_state_dir_sane(project / ".lockstep", project)
    with pytest.raises(RunnerError):
        assert_state_dir_sane(project, project)   # equal IS inside the tree
    assert_state_dir_sane(tmp_path / "state", project)  # sibling: sane, no raise


def test_state_dir_symlink_into_project_is_rejected(tmp_path):
    from lockstep_mcp.runners import assert_state_dir_sane

    project = tmp_path / "proj"
    (project / "inner").mkdir(parents=True)
    link = tmp_path / "innocent-looking-state"
    link.symlink_to(project / "inner")
    with pytest.raises(RunnerError):
        assert_state_dir_sane(link, project)


def test_alias_spelling_of_project_is_still_inside(tmp_path):
    # Path.resolve() does not case-canonicalize and
    # PosixPath comparison is case-sensitive, so a case-variant spelling of
    # the project (APFS/NTFS) evades string ancestry — only stat identity
    # (device+inode) catches it. Runtime-probe the filesystem: use the case
    # variant where it aliases the same dir, else a symlink alias — both
    # spellings resolve to the same inode, which is what samestat sees.
    from lockstep_mcp.runners import assert_state_dir_sane

    project = tmp_path / "proj"
    project.mkdir()
    alias = tmp_path / "PROJ"
    if not alias.exists():                        # case-SENSITIVE fs
        alias = tmp_path / "alias"
        alias.symlink_to(project)
    with pytest.raises(RunnerError):
        assert_state_dir_sane(alias / "state", project)


def test_home_shaped_state_dir_still_sane(tmp_path):
    from lockstep_mcp.runners import assert_state_dir_sane

    home = tmp_path / "home"
    (home / "work" / "proj").mkdir(parents=True)
    # the default ~/.lockstep shape: state under $HOME, project deeper in a
    # sibling branch — sane (the state dir need not even exist yet)
    assert_state_dir_sane(home / ".lockstep", home / "work" / "proj")
    with pytest.raises(RunnerError):
        assert_state_dir_sane(home / ".lockstep", home)  # project IS $HOME: refuse


def test_engine_start_refuses_state_dir_inside_project(tmp_path):
    from lockstep_mcp.engine import Engine

    project = tmp_path / "proj"
    project.mkdir()
    eng = Engine(project / ".lockstep", tmp_path / "recipes")
    with pytest.raises(RunnerError):
        eng.start("anything", {}, str(project))


# ---------------------------------------------------------------------------
# child env — exact allowlist, all-or-nothing credential
# ---------------------------------------------------------------------------


def test_child_env_exact_allowlist(tmp_path):
    base = {
        "PATH": "/bin", "HOME": "/home/u", "SystemRoot": "C:\\Windows",
        "SHELL": "/bin/zsh",                       # dropped: a -p child needs no shell
        "SECRET_TOKEN": "leak", "ANTHROPIC_API_KEY": "leak2",
        "LOCKSTEP_RECIPES": "/recipes",            # passthrough: fractal child, same recipes
        "LOCKSTEP_CHILD_NONCE": "old", "LOCKSTEP_STATE_DIR": "/should/be/overridden",
    }
    env = child_env(base, tmp_path, "run-1", "n1")
    assert env == {
        "PATH": "/bin", "HOME": "/home/u", "SystemRoot": "C:\\Windows",
        "LOCKSTEP_RECIPES": "/recipes",
        "LOCKSTEP_STATE_DIR": str(tmp_path),       # preserved-and-pinned: shared index
        "LOCKSTEP_CHILD_RUN": "run-1", "LOCKSTEP_CHILD_NONCE": "n1",
    }


def test_child_env_one_shot_has_no_credentials(tmp_path):
    env = child_env(
        {"PATH": "/bin", "ANTHROPIC_API_KEY": "leak",
         "LOCKSTEP_CHILD_RUN": "stale", "LOCKSTEP_CHILD_NONCE": "stale"},
        tmp_path, None, None,
    )
    assert env == {"PATH": "/bin", "LOCKSTEP_STATE_DIR": str(tmp_path)}


def test_partial_child_credential_refuses(tmp_path):
    with pytest.raises(RunnerError):
        child_env({"PATH": "/bin"}, tmp_path, "run-1", None)
    with pytest.raises(RunnerError):
        child_env({"PATH": "/bin"}, tmp_path, None, "n1")


def test_depth2_child_inherits_the_adapter_runner_default(tmp_path):
    # LOCKSTEP_RUNNER is a NAME resolved against the owner allowlist,
    # not a path — it must survive child_env so a depth-2 child whose
    # markers rely on the adapter default can still spawn. Fails against
    # the pre-fix allowlist (which dropped it): resolve() raises
    # "no runner named".
    exe = _fake_exe(tmp_path); _write_runners(tmp_path, exe)
    env = child_env({"PATH": "/bin", "LOCKSTEP_RUNNER": "claude"}, tmp_path, "run-1", "n1")
    assert env["LOCKSTEP_RUNNER"] == "claude"
    spec = resolve(tmp_path, None, env)                    # marker names no runner
    assert spec.name == "claude"


def test_misspelled_top_level_budget_key_is_refused(tmp_path):
    # The looser default is what a silent fallback means here: an owner
    # writing `max_fractal_deph: 0` to switch fractal children OFF would get
    # depth 2 in force and nothing said.
    exe = _fake_exe(tmp_path)
    (tmp_path / "runners.yaml").write_text(
        "runners:\n"
        f"  claude: {{path: {exe}, models: [m]}}\n"
        "budgets: {max_fractal_deph: 0}\n"
    )
    with pytest.raises(RunnerError) as exc:
        load_runners(tmp_path)
    assert "max_fractal_deph" in str(exc.value)
