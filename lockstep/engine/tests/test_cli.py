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

import builtins
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lockstep import __version__, cli
from lockstep.recipe.loader import RecipeLoader

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def test_cli_preserves_the_public_authoring_error_identity() -> None:
    from lockstep import authoring

    assert cli.AuthoringError is authoring.AuthoringError
    assert cli.CliError is authoring.AuthoringError


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

    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "recipes").mkdir()
    assert cli.main(["doctor"]) == 0


def test_consent_issue_and_revoke_require_an_interactive_owner_tty_before_service(
    monkeypatch, capsys
) -> None:
    from lockstep.runtime import engine as engine_module

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        engine_module.Engine,
        "command",
        lambda *_args, **_kwargs: pytest.fail("non-TTY consent constructed service"),
    )

    assert cli.main(
        ["consent", "issue", "--run", "run-1", "--step", "accept-review"]
    ) == 2
    assert "TTY" in capsys.readouterr().err
    assert cli.main(["consent", "revoke"]) == 2
    assert "TTY" in capsys.readouterr().err


def test_consent_issue_previews_exact_commitment_and_prints_token_once(
    tmp_path, monkeypatch, capsys
) -> None:
    from lockstep.runtime import engine as engine_module

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    digest = "a" * 64
    calls = []

    class FakeEngine:
        def preview_publication_consent(self, run_id, step, *, project):
            calls.append(("preview", run_id, step, project))
            return {
                "public_run_id": run_id,
                "artifact_ref": "artifact:" + "b" * 64,
                "artifact_digest": "c" * 64,
                "destination": "docs/review.md",
                "transformation": "identity",
                "audience": "local-project",
                "digest": digest,
            }

        def issue_publication_consent(
            self, run_id, step, expected_commitment_digest, *, project
        ):
            calls.append(
                ("issue", run_id, step, expected_commitment_digest, project)
            )
            return SimpleNamespace(token="one-time-secret-token")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(engine_module.Engine, "command", lambda *_args: FakeEngine())
    monkeypatch.setattr(builtins, "input", lambda _prompt="": digest)

    assert cli.main(
        ["consent", "issue", "--run", "run-1", "--step", "accept-review"]
    ) == 0
    output = capsys.readouterr().out
    assert digest in output
    assert "docs/review.md" in output
    assert output.count("one-time-secret-token") == 1
    assert calls == [
        ("preview", "run-1", "accept-review", str(project.resolve())),
        ("issue", "run-1", "accept-review", digest, str(project.resolve())),
        ("close",),
    ]


def test_consent_issue_confirmation_mismatch_never_mints(tmp_path, monkeypatch, capsys) -> None:
    from lockstep.runtime import engine as engine_module

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    calls = []

    class FakeEngine:
        def preview_publication_consent(self, run_id, step, *, project):
            calls.append(("preview", run_id, step, project))
            return {"digest": "a" * 64, "destination": "docs/review.md"}

        def issue_publication_consent(self, *_args, **_kwargs):
            pytest.fail("mismatched confirmation minted consent")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(engine_module.Engine, "command", lambda *_args: FakeEngine())
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "b" * 64)

    assert cli.main(
        ["consent", "issue", "--run", "run-1", "--step", "accept-review"]
    ) == 2
    assert "cancelled" in capsys.readouterr().err
    assert calls == [
        ("preview", "run-1", "accept-review", str(project.resolve())),
        ("close",),
    ]


@pytest.mark.parametrize(
    "forbidden",
    ["--artifact", "--destination", "--generation", "--consent-ref", "--token", "--yes"],
)
def test_consent_issue_parser_has_no_noninteractive_or_caller_authority_escape(
    forbidden: str,
) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "consent",
                "issue",
                "--run",
                "run-1",
                "--step",
                "accept-review",
                forbidden,
                "forged",
            ]
        )


def test_consent_accept_reads_hidden_token_and_forwards_only_token_and_cwd(
    tmp_path, monkeypatch, capsys
) -> None:
    import getpass
    from lockstep.runtime import engine as engine_module

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    calls = []

    class FakeEngine:
        def scenario_accept_artifact(self, token, *, project):
            calls.append(("accept", token, project))
            return {"status": "completed", "run_id": "run-1"}

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(engine_module.Engine, "command", lambda *_args: FakeEngine())
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": "hidden-token")

    assert cli.main(["consent", "accept"]) == 0
    assert calls == [
        ("accept", "hidden-token", str(project.resolve())),
        ("close",),
    ]
    assert "hidden-token" not in capsys.readouterr().out


def test_consent_accept_reads_one_bounded_piped_line_and_revoke_scopes_to_cwd(
    tmp_path, monkeypatch, capsys
) -> None:
    from lockstep.runtime import engine as engine_module

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    calls = []

    class FakeEngine:
        def scenario_accept_artifact(self, token, *, project):
            calls.append(("accept", token, project))
            return {"status": "completed", "run_id": "run-1"}

        def revoke_publication_consents(self, *, project):
            calls.append(("revoke", project))
            return 4

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(engine_module.Engine, "command", lambda *_args: FakeEngine())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("piped-token\nignored\n"))
    assert cli.main(["consent", "accept"]) == 0
    assert calls[:2] == [
        ("accept", "piped-token", str(project.resolve())),
        ("close",),
    ]

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda _prompt="": f"REVOKE {project.resolve()}",
    )
    assert cli.main(["consent", "revoke"]) == 0
    assert calls[2:] == [
        ("revoke", str(project.resolve())),
        ("close",),
    ]
    assert "epoch 4" in capsys.readouterr().out


def test_consent_accept_allows_one_maximum_bounded_piped_token(
    tmp_path, monkeypatch
) -> None:
    from lockstep.runtime import engine as engine_module

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    token = "x" * 4096
    calls = []

    class FakeEngine:
        def scenario_accept_artifact(self, actual, *, project):
            calls.append((actual, project))
            return {"status": "completed"}

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(engine_module.Engine, "command", lambda *_args: FakeEngine())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(f"{token}\nignored\n"))

    assert cli.main(["consent", "accept"]) == 0
    assert calls == [(token, str(project.resolve())), ("close",)]


@pytest.mark.parametrize(
    "arguments",
    (
        ("recipe", "compile", "release"),
        ("recipe", "check", "release"),
        ("recipe", "diff", "release"),
        ("recipe", "render", "release", "--view", "workflow"),
        ("recipe", "estimate", "release"),
    ),
)
def test_installed_cli_refuses_legacy_authoring_evidence(
    tmp_path, monkeypatch, capsys, arguments
) -> None:
    from lockstep import authoring
    from tests._authoring_gate import tree_image, write_workflow
    from tests.test_authoring_legacy_v4_refusal import (
        _locate_test_namespace,
        _retain,
        live_v4_bytes,
    )

    project = tmp_path / "project"
    project.mkdir()
    state = (tmp_path / "state").resolve()
    write_workflow(project, "release")
    authoring.publish_project_compilation(project, "release", state_dir=state)
    namespace, _identity = _locate_test_namespace(state, project)
    transaction = _retain(namespace, live_v4_bytes(project))
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    before = transaction.read_bytes(); project_before, owner_before = tree_image(project), tree_image(state)

    assert cli.main(list(arguments)) == 2
    error = capsys.readouterr().err
    assert "pre-simplification" in error
    assert str(project.resolve()) in error and str(state) in error
    assert "Do not delete transaction.json manually" in error
    assert transaction.read_bytes() == before
    assert tree_image(project) == project_before and tree_image(state) == owner_before
