import os
from pathlib import Path

import pytest

from lockstep.runtime.manifests import (
    ProjectWritePath,
    capture_project,
    compare_effect,
)
from lockstep.runtime.sandbox import (
    FakeSandboxProvider,
    SandboxAttestation,
    SandboxPolicy,
    spawn_verified,
)
from lockstep.runtime.validators import run_checks


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_effect_gate_checks_pass_fail_and_error(project: Path) -> None:
    before = capture_project(project)
    (project / "outside.txt").write_text("x")

    for outcome in ("pass", "fail", "error"):
        result = compare_effect(before, capture_project(project), [], outcome)
        assert result.integrity_error is True
        assert "outside.txt" in result.reasons[0]


def test_effect_gate_accepts_only_declared_prefix_and_rejects_symlink_output(project: Path, tmp_path: Path) -> None:
    before = capture_project(project)
    allowed = [ProjectWritePath.parse("reports/", project)]
    (project / "reports").mkdir()
    (project / "reports" / "ok.md").write_text("ok")
    assert compare_effect(before, capture_project(project), allowed, "pass").integrity_error is False

    (project / "reports" / "replaced.md").write_text("regular")
    before = capture_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "reports" / "replaced.md").unlink()
    os.symlink(outside, project / "reports" / "replaced.md")
    result = compare_effect(before, capture_project(project), allowed, "fail")
    assert result.integrity_error is True
    assert "symlink" in result.reasons[0]


def test_effect_gate_rejects_deleted_or_replaced_symlink_even_when_its_path_is_allowed(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = [ProjectWritePath.parse("reports/", project)]
    (project / "reports").mkdir()
    link = project / "reports" / "link"
    os.symlink(outside, link)
    before = capture_project(project)
    link.unlink()

    deleted = compare_effect(before, capture_project(project), allowed, "pass")
    assert deleted.integrity_error is True
    assert "symlink" in deleted.reasons[0]

    os.symlink(outside, link)
    before = capture_project(project)
    link.unlink()
    link.write_text("replacement")
    replaced = compare_effect(before, capture_project(project), allowed, "pass")
    assert replaced.integrity_error is True
    assert "symlink" in replaced.reasons[0]


def test_effect_gate_detects_git_attestation_mutation_for_every_outcome(project: Path) -> None:
    git = project / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    before = capture_project(project)
    (git / "HEAD").write_text("ref: refs/heads/other\n")

    for outcome in ("pass", "fail", "error"):
        result = compare_effect(before, capture_project(project), [], outcome)
        assert result.integrity_error is True
        assert result.reasons == ("integrity: Git control state changed",)


def test_validator_converts_effect_violation_to_integrity_error_for_all_outcomes(project: Path) -> None:
    before = capture_project(project)
    (project / "unexpected.txt").write_text("x")
    state = {
        "brief": {"checks": [{"type": "file_exists", "path": "unexpected.txt"}]},
        "evidence": {},
        "_project": str(project),
        "_effect_before": before,
        "_effect_allowed": [],
    }

    for outcome in ("pass", "fail", "error"):
        state["_effect_outcome"] = outcome
        verdict = run_checks(state, execute=True)
        assert verdict["verdict_status"] == "error"
        assert "integrity" in verdict["verdict_reasons"][0]


def test_valid_effect_pass_is_the_only_outcome_that_can_advance_a_baseline(project: Path) -> None:
    before = capture_project(project)
    (project / "report.md").write_text("ok")
    allowed = [ProjectWritePath.parse("report.md", project)]

    assert compare_effect(before, capture_project(project), allowed, "pass").baseline_eligible is True
    assert compare_effect(before, capture_project(project), allowed, "fail").baseline_eligible is False
    assert compare_effect(before, capture_project(project), allowed, "error").baseline_eligible is False


def test_fake_sandbox_binds_policy_and_never_uses_a_shell(project: Path) -> None:
    policy = SandboxPolicy(
        read_roots=(project,),
        write_root=project,
        temp_root=project / ".tmp",
        argv=("tool", "argument with spaces"),
        cwd=project,
        environment=(("PATH", "/trusted/bin"),),
    )
    provider = FakeSandboxProvider()

    attestation = provider.preflight(policy)
    handle = provider.spawn(policy, policy.argv, stdin=b"prompt")

    assert attestation.policy_digest == policy.digest
    assert handle.argv == ("tool", "argument with spaces")
    assert handle.stdin == b"prompt"
    with pytest.raises(ValueError, match="policy"):
        provider.spawn(policy, ["other-tool"])


def test_sandbox_policy_digest_binds_argv_cwd_and_sanitized_environment(project: Path) -> None:
    common = {
        "read_roots": (project,),
        "write_root": project,
        "temp_root": project / ".tmp",
        "argv": ("tool",),
        "cwd": project,
    }
    base = SandboxPolicy(**common, environment=(("PATH", "/trusted/bin"),))

    assert SandboxPolicy(**{**common, "argv": ("other",), "environment": base.environment}).digest != base.digest
    assert SandboxPolicy(**{**common, "cwd": project / "other", "environment": base.environment}).digest != base.digest
    assert SandboxPolicy(**common, environment=(("PATH", "/other/bin"),)).digest != base.digest


@pytest.mark.parametrize(
    "attestation",
    [
        SandboxAttestation("fake", "1", "wrong", True, True, True),
        SandboxAttestation("fake", "1", "", False, True, True),
        SandboxAttestation("fake", "1", "", True, False, True),
        SandboxAttestation("fake", "1", "", True, True, False),
    ],
)
def test_verified_sandbox_spawn_rejects_invalid_attestation_before_process_handle(
    project: Path, attestation: SandboxAttestation
) -> None:
    policy = SandboxPolicy(
        read_roots=(project,), write_root=project, temp_root=project / ".tmp",
        argv=("tool",), cwd=project, environment=(("PATH", "/trusted/bin"),),
    )

    class BadProvider(FakeSandboxProvider):
        spawned = False

        def preflight(self, _: SandboxPolicy) -> SandboxAttestation:
            return SandboxAttestation(
                attestation.provider_id,
                attestation.provider_version,
                policy.digest if not attestation.policy_digest else attestation.policy_digest,
                attestation.denies_outside_workspace,
                attestation.denies_vcs_write,
                attestation.denies_symlink_escape,
            )

        def spawn(self, *args, **kwargs):
            self.spawned = True
            return super().spawn(*args, **kwargs)

    provider = BadProvider()
    with pytest.raises(ValueError, match="attestation"):
        spawn_verified(provider, policy, policy.argv)
    assert provider.spawned is False


def test_verified_sandbox_spawn_rejects_unbound_argv_and_bad_returned_handle(project: Path) -> None:
    policy = SandboxPolicy(
        read_roots=(project,), write_root=project, temp_root=project / ".tmp",
        argv=("tool",), cwd=project, environment=(("PATH", "/trusted/bin"),),
    )

    class PermissiveProvider(FakeSandboxProvider):
        spawned = False

        def spawn(self, *args, **kwargs):
            self.spawned = True
            return super().spawn(*args, **kwargs)

    provider = PermissiveProvider()
    with pytest.raises(ValueError, match="argv"):
        spawn_verified(provider, policy, ("other",))
    assert provider.spawned is False

    class BadHandleProvider(FakeSandboxProvider):
        def spawn(self, *args, **kwargs):
            return type("BadHandle", (), {"argv": ("other",), "policy_digest": "wrong"})()

    with pytest.raises(ValueError, match="process handle"):
        spawn_verified(BadHandleProvider(), policy, policy.argv)
