from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.providers.base import EffectRequest, RunnerAdapter
from tests.runtime.providers.fakes import FakeRunner


def _request(binding_digest: str) -> EffectRequest:
    intent = EffectRequest.build(
        effect_id="eff_contract",
        public_run_id="run-contract",
        project_identity="project-contract",
        definition_digest="a" * 64,
        coordinate=NativeCoordinate("thread", "checkpoint", "", "task", "interrupt"),
        descriptor_digest="b" * 64,
        effect_kind="managed",
        runner_selector="runner",
        runner_binding_digest=binding_digest,
        required_capabilities=("workspace", "bounded_result", "sandbox"),
        inputs=(("brief", "make the requested change"),),
        writes=("src/",),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref="workspace:contract",
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=1,
        approval_generation=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return intent.bind_grant(grant)


def assert_provider_contract(runner: RunnerAdapter) -> None:
    request = _request(runner.binding_digest)
    first = runner.prepare(request)
    second = runner.prepare(request)

    assert first == second
    assert first.effect_id == request.effect_id
    assert first.request_digest == request.request_digest
    assert first.runner_binding_digest == runner.binding_digest

    runner.ensure_started(first)
    runner.ensure_started(first)
    observed = runner.inspect(request.effect_id)
    assert observed.effect_id == request.effect_id
    assert observed.request_digest == request.request_digest
    assert observed.runner_binding_digest == runner.binding_digest


def test_provider_neutral_contract_accepts_claude_feasibility_fake() -> None:
    runner = FakeRunner()
    assert_provider_contract(runner)
    assert runner.spawn_count == 1


def test_provider_contract_values_have_no_codex_or_git_fields() -> None:
    request_fields = set(EffectRequest.__dataclass_fields__)
    assert not request_fields.intersection(
        {
            "argv",
            "environment",
            "permission_profile",
            "codex_home",
            "git_dir",
            "worktree_path",
            "result_spool",
        }
    )
