from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep import cli
from lockstep.mcp import server
from lockstep.runtime import read_resources
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.execution_evidence import project_execution_evidence
from lockstep.runtime.native_models import NativeSnapshot
from lockstep.runtime.owner_state import InsecureStatePath
from lockstep.runtime.providers.base import PreparedLaunch, launch_commitment_digest
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.storage import SQLiteStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"
EXECUTION_EVIDENCE_EXEMPLARS = (
    Path(__file__).parents[1] / "fixtures" / "task13_execution_evidence_exemplars.jsonl"
)


def test_all_eight_normative_execution_evidence_exemplars_are_digest_valid() -> None:
    # Extracted from Task 13 evidence schemas spec SHA-256 02086bd7231b8855e746fa377caac36434504e1e4234a54c4e86fae587c1924c.
    fixture = EXECUTION_EVIDENCE_EXEMPLARS.read_bytes()
    assert fixture.endswith(b"\n")
    exemplars = fixture.decode("utf-8").splitlines()
    assert len(exemplars) == 8
    assert all(exemplars)

    for line in exemplars:
        value = json.loads(line)
        assert json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) == line
        root = {key: item for key, item in value.items() if key != "projection_digest"}
        assert value["projection_digest"] == hashlib.sha256(
            json.dumps(root, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for run in value["runs"]:
            for effect in run["effects"]:
                launch = effect["launch"]
                terminal = None if launch is None else launch["terminal"]
                if terminal is not None:
                    safe = {
                        key: item
                        for key, item in terminal.items()
                        if key != "terminal_ref"
                    }
                    assert terminal["terminal_ref"] == hashlib.sha256(
                        b"lockstep-public-terminal-v1\0"
                        + json.dumps(
                            safe, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                publication = effect["publication"]
                for item in () if publication is None else publication["items"]:
                    commitment = item["commitment"]
                    unsigned = {
                        key: part
                        for key, part in commitment.items()
                        if key != "digest"
                    }
                    assert commitment["digest"] == hashlib.sha256(
                        json.dumps(
                            unsigned, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest()


def _context(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"workspaces": {str(project): {}}}}
        )
    )


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    child = (FIXTURES / "worker_child_interrupt.recipe.yaml").read_text()
    (recipes / "child_interrupt.recipe.yaml").write_text(
        child.replace("name: native-child-interrupt", "name: child_interrupt")
    )
    server._reset_engine()
    return project


def _stop_pump(service: LockstepCommandService) -> None:
    service._activate_writable_core()  # noqa: SLF001 - crash-boundary fixture
    service._pump_stop.set()  # noqa: SLF001 - deterministic real crash boundary
    service._pump_wakeup.set()  # noqa: SLF001
    thread = service._pump_thread  # noqa: SLF001
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _seed_recoverable_run(project: Path, state: Path, recipes: Path) -> str:
    """Leave a real admitted start watch before its first native checkpoint."""

    service = LockstepCommandService(state, recipes)
    _stop_pump(service)
    real_start = service.runtime.ensure_started

    def crash_before_first_checkpoint(_run_id, _values):
        raise RuntimeError("crash before first checkpoint")

    service.runtime.ensure_started = crash_before_first_checkpoint
    try:
        with pytest.raises(RuntimeError, match="crash before first checkpoint"):
            service.start("native-parent-direct", {}, str(project))
        bindings = service.catalog.list(str(project.resolve()))
        assert len(bindings) == 1
        high_water = service.effects.max_run_drive_admission_seq()
        assert high_water is not None
        watches = service.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=2,
        )
        assert [watch.public_run_id for watch in watches] == [
            bindings[0].public_run_id
        ]
        return bindings[0].public_run_id
    finally:
        service.runtime.ensure_started = real_start
        service.close()


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    return tuple(
        (str(path.relative_to(root)), path.stat().st_mode & 0o777, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm", "-journal"))
    )


def _changed_paths(
    before: tuple[tuple[str, int, bytes], ...],
    after: tuple[tuple[str, int, bytes], ...],
) -> tuple[str, ...]:
    old = {path: (mode, content) for path, mode, content in before}
    new = {path: (mode, content) for path, mode, content in after}
    return tuple(
        path for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)
    )


def _empty_execution_evidence(project: Path) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "lockstep.execution-evidence/v1",
        "project_identity": str(project.resolve()),
        "selected_run_id": None,
        "runs": [],
        "unmatched_launches": [],
    }
    value["projection_digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


@pytest.mark.parametrize(
    "value",
    ("bad\x01text", "bad\x85text", "e\N{COMBINING ACUTE ACCENT}"),
)
def test_execution_evidence_rejects_noncanonical_public_text(value: str) -> None:
    from lockstep.runtime.execution_evidence import _text

    with pytest.raises(ValueError, match="bounded text"):
        _text(value, "candidate")


@pytest.mark.parametrize(
    "value",
    ("a\\b", "a//b", "a/./b", "a/../b", "x" * 513),
)
def test_execution_evidence_rejects_noncanonical_safe_paths(value: str) -> None:
    from lockstep.runtime.execution_evidence import _relative_path

    with pytest.raises(ValueError, match="safe relative path"):
        _relative_path(value, "candidate")


def test_execution_evidence_rejects_global_effect_budget_before_native_projection(
    tmp_path: Path,
) -> None:
    class OverBudgetResources:
        def effect_count_for_threads(self, thread_ids, *, limit):
            assert tuple(thread_ids) == ("thread-a", "thread-b")
            assert limit == 10_000
            return 10_001

        def effects_for_thread(self, _thread_id):
            pytest.fail("over-budget evidence reached effect materialization")

        @contextmanager
        def native_app(self, _binding):
            pytest.fail("over-budget evidence reached native projection")
            yield

    project = tmp_path / "project"
    project.mkdir()
    bindings = tuple(
        SimpleNamespace(
            thread_id=thread_id,
            public_run_id=f"run-{suffix}-{'a' * 32}",
            project_identity=str(project.resolve()),
            recipe_digest="a" * 64,
            recipe_snapshot_ref="snapshot",
        )
        for thread_id, suffix in (("thread-a", "a"), ("thread-b", "b"))
    )
    with pytest.raises(ValueError, match="effect limit"):
        project_execution_evidence(
            OverBudgetResources(),
            state_dir=tmp_path / "owner",
            project_identity=str(project.resolve()),
            selected_run_id=None,
            bindings=bindings,
        )


@pytest.mark.parametrize(
    "encoded",
    ('{"a":1,"a":2}', '{"a": 1}', '{"b":2,"a":1}'),
)
def test_passive_effect_results_reject_duplicate_or_noncanonical_json(
    encoded: str,
) -> None:
    from lockstep.runtime.read_resources import _result_object

    with pytest.raises(ValueError, match="canonical|duplicate"):
        _result_object(encoded)


def test_scenario_evidence_cli_and_mcp_return_the_same_empty_read_only_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    state = tmp_path / "owner-state"
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    server._reset_engine()

    assert cli.main(["scenario", "evidence"]) == 0
    cli_bytes = capsys.readouterr().out
    cli_value = json.loads(cli_bytes)
    mcp_value = server.scenario_evidence(ctx=_context(project))

    expected = _empty_execution_evidence(project)
    assert cli_value == expected
    assert cli_bytes == (
        json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    assert mcp_value == cli_value
    assert not state.exists()


def test_scenario_evidence_selected_unknown_run_fails_without_materializing_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"

    projection = Engine.observe(state, recipes)
    with pytest.raises(LockstepError, match="unknown run 'missing'"):
        projection.evidence("missing", str(project))
    assert not state.exists()


def test_scenario_evidence_rejects_foreign_project_without_byte_or_mtime_change(
    tmp_path: Path,
) -> None:
    owned_project = tmp_path / "owned-project"
    foreign_project = tmp_path / "foreign-project"
    recipes = foreign_project / "recipes"
    owned_project.mkdir()
    recipes.mkdir(parents=True)
    state = tmp_path / "owner-state"
    store = SQLiteStore(state / "runtime.sqlite")
    run_id = "task13-foreign-" + "a" * 32
    RunCatalog(store).create(
        RunBinding(
            run_id,
            "thread-foreign",
            "b" * 64,
            "snapshot:" + "c" * 64,
            str(owned_project.resolve()),
            "2026-09-01T12:00:00+00:00",
        )
    )
    before = _tree_snapshot(state)

    with pytest.raises(LockstepError, match=f"unknown run {run_id!r}"):
        Engine.observe(state, recipes).evidence(run_id, str(foreign_project))

    assert _tree_snapshot(state) == before


def _seed_evidence_effects(
    state: Path, project: Path, *, scope_phase: str = "delivered"
) -> tuple[str, str]:
    run_id = "task13-evidence-" + "1" * 32
    thread_id = "thread-evidence"
    store = SQLiteStore(state / "runtime.sqlite")
    RunCatalog(store).create(
        RunBinding(
            run_id,
            thread_id,
            "a" * 64,
            "snapshot:" + "b" * 64,
            str(project.resolve()),
            "2026-09-01T12:00:00+00:00",
        )
    )
    kinds = ("managed", "manual", "pinned", "verify", "decide", "accept", "publish", "scope")
    with store.write_transaction() as connection:
        for index, kind in enumerate(kinds):
            connection.execute(
                store.tables.effects.insert().values(
                    effect_id=f"effect-{kind}",
                    thread_id=thread_id,
                    checkpoint_ns="",
                    checkpoint_id="checkpoint-earlier",
                    task_id=f"task-{index}",
                    interrupt_id=f"interrupt-{index}",
                    descriptor_digest=f"{index + 1:x}" * 64,
                    effect_kind=kind,
                    deadline_at=None,
                    phase=scope_phase if kind == "scope" else "sealed",
                    lease_epoch=0,
                    runner_binding_digest=None,
                    workspace_ref=None,
                    request_digest=None,
                    grant_digest=None,
                    launch_commitment_digest=None,
                    result_ref=None,
                    fixed_error_code=None,
                    created_at="2026-09-01T12:00:00+00:00",
                    updated_at="2026-09-01T12:00:00.1+00:00",
                    revision=0,
                )
            )
    store.close()
    return run_id, thread_id


class _EvidenceNativeApp:
    def __init__(self, thread_id: str, *, lineage: bool = True) -> None:
        self.thread_id = thread_id
        self.lineage = lineage
        self.ancestor_calls: list[dict[str, object]] = []

    def snapshot(self, *, thread_id: str, subgraphs: bool = False) -> NativeSnapshot:
        assert thread_id == self.thread_id
        assert subgraphs is True
        return NativeSnapshot(
            {"lockstep_outcome": "PASS"},
            checkpoint_id="checkpoint-current",
            checkpoint_ns="",
            created_at="2026-09-01T12:01:00+00:00",
        )

    def checkpoint_is_ancestor(self, **values) -> bool:
        self.ancestor_calls.append(values)
        assert values["ancestor_checkpoint_id"] == "checkpoint-earlier"
        assert values["descendant_checkpoint_id"] == "checkpoint-current"
        return self.lineage


def test_execution_evidence_projects_every_effect_kind_once_and_checks_lineage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(state, project)
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    value = projection.evidence(run_id, str(project))

    effects = value["runs"][0]["effects"]
    assert [item["effect_kind"] for item in effects] == [
        "managed",
        "manual",
        "pinned",
        "verify",
        "decide",
        "accept",
        "publish",
        "scope",
    ]
    assert len({item["effect_id"] for item in effects}) == 8
    assert all(
        set(item)
        == {
            "acceptance",
            "coordinate",
            "descriptor_digest",
            "effect_id",
            "effect_kind",
            "launch",
            "phase",
            "publication",
            "updated_at",
        }
        for item in effects
    )
    assert all(
        item["launch"] is None
        and item["acceptance"] is None
        and item["publication"] is None
        for item in effects
    )
    assert len(app.ancestor_calls) == 8
    assert value["runs"][0]["checkpoint"] == {
        "checkpoint_id": "checkpoint-current",
        "checkpoint_ns": "",
        "created_at": "2026-09-01T12:01:00.000000Z",
        "status": "completed",
    }
    digest_body = {key: item for key, item in value.items() if key != "projection_digest"}
    assert value["projection_digest"] == hashlib.sha256(
        json.dumps(digest_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_execution_evidence_rejects_invalid_scope_phase_and_inverse_lineage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(
        state, project, scope_phase="running"
    )
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    with pytest.raises(LockstepError, match="trusted native state"):
        projection.evidence(run_id, str(project))

    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "UPDATE effects SET phase = 'delivered' WHERE effect_kind = 'scope'"
        )
        connection.commit()
    finally:
        connection.close()
    app.lineage = False
    with pytest.raises(LockstepError, match="trusted native state"):
        projection.evidence(run_id, str(project))


def test_execution_evidence_projects_only_the_closed_acceptance_result(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(state, project)
    acceptance = {
        "schema": "lockstep.acceptance-result/v1",
        "effect_id": "effect-accept",
        "outcome": "PASS",
        "artifact_ref": "artifact:review",
        "artifact_digest": "d" * 64,
        "destination": ".lockstep/review.md",
        "transformation": "identity",
        "audience": "local-project",
        "consent_ref": "consent:review",
        "approval_generation": 1,
        "receipt_digest": "e" * 64,
    }
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "INSERT INTO effect_observations "
            "(effect_id, revision, phase, result_json, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "effect-accept",
                1,
                "delivered",
                json.dumps(acceptance, sort_keys=True, separators=(",", ":")),
                "2026-09-01T12:00:01+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    value = projection.evidence(run_id, str(project))
    projected = next(
        item
        for item in value["runs"][0]["effects"]
        if item["effect_kind"] == "accept"
    )
    assert projected["acceptance"] == acceptance
    assert projected["launch"] is None
    assert projected["publication"] is None


def _write_owner_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_owner_document(path: Path, value: object) -> None:
    path.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    path.chmod(0o600)


def _projected_strings(value: object, path: tuple[object, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _projected_strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _projected_strings(item, (*path, index))
    elif isinstance(value, str):
        yield path, value


def test_actual_engine_evidence_is_invariant_across_private_disclosure_domains(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(state, project)
    effect_id = "effect-managed"
    workspace_ref = "workspace:disclosure"
    grant_digest = "8" * 64
    public_launch_ref = "c" * 64
    start_ref = "e" * 64
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    executable = "/usr/local/bin/codex"
    argv = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-test",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(workspace),
        "-",
    ]
    attempts = state / "codex-attempts"
    attempts.mkdir(mode=0o700)
    attempt = attempts / hashlib.sha256(effect_id.encode()).hexdigest()
    attempt.mkdir(mode=0o700)
    safe_start = {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": effect_id,
        "public_launch_ref": public_launch_ref,
        "start_ref": start_ref,
    }
    terminal = {
        "effect_id": effect_id,
        "overflow": False,
        "public_launch_ref": public_launch_ref,
        "quiescent": True,
        "returncode": 0,
        "start_ref": start_ref,
        "termination_reason": "exited",
        "timed_out": False,
    }
    terminal["terminal_ref"] = hashlib.sha256(
        b"lockstep-public-terminal-v1\0"
        + json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    public_paths = (
        attempt / "spawn-fence.json",
        attempt / "public-start.json",
        attempt / "public-terminal.json",
    )
    _write_owner_document(public_paths[0], safe_start)
    _write_owner_document(public_paths[1], safe_start)
    _write_owner_document(public_paths[2], terminal)
    public_identity = tuple(
        (path.read_bytes(), path.stat().st_mtime_ns) for path in public_paths
    )
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    expected_projection = None
    expected_terminal_ref = None
    retained_surface = b""
    retained_strings: tuple[tuple[tuple[object, ...], str], ...] = ()
    for index in range(128):
        credential_digest = hashlib.sha256(
            f"credential-{index}".encode()
        ).hexdigest()
        runner_digest = hashlib.sha256(f"runner-{index}".encode()).hexdigest()
        private_launch_ref = "codex:" + hashlib.sha256(
            f"launch-{index}".encode()
        ).hexdigest()
        request_digest = hashlib.sha256(f"request-{index}".encode()).hexdigest()
        native_commitment = launch_commitment_digest(
            SimpleNamespace(grant_digest=grant_digest),
            PreparedLaunch(
                effect_id,
                request_digest,
                runner_digest,
                private_launch_ref,
                workspace_ref,
            ),
        )
        connection = sqlite3.connect(state / "runtime.sqlite")
        try:
            connection.execute(
                "UPDATE effects SET runner_binding_digest = ?, workspace_ref = ?, "
                "request_digest = ?, grant_digest = ?, launch_commitment_digest = ? "
                "WHERE effect_id = ?",
                (
                    runner_digest,
                    workspace_ref,
                    request_digest,
                    grant_digest,
                    native_commitment,
                    effect_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        _write_owner_json(
            attempt / "launch.json",
            {
                "schema": "lockstep.codex-launch/v1",
                "effect_id": effect_id,
                "request_digest": request_digest,
                "runner_binding_digest": runner_digest,
                "workspace_ref": workspace_ref,
                "workspace_path": str(workspace),
                "workspace_purpose": "managed_output",
                "execution_class": "managed-agent",
                "cwd": str(workspace),
                "executable_path": executable,
                "executable_identity_digest": runner_digest,
                "inner_argv": argv,
                "environment": [["SECRET_ENV", f"private-{index}"]],
                "codex_home": str(tmp_path / f"secret-codex-home-{index}"),
                "credential_identity_digest": credential_digest,
                "sandbox_policy_digest": "5" * 64,
                "sandbox_attestation_digest": "4" * 64,
                "launcher_decision_generation": 1,
                "deadline_at": "2026-09-01T13:00:00+00:00",
                "launch_ref": private_launch_ref,
                "public_launch_ref": public_launch_ref,
                "start_ref": start_ref,
                "shell": False,
                "close_fds": True,
                "inherited_fds": [],
                "deployment_profile": "local_unsandboxed",
            },
        )
        value = projection.evidence(run_id, str(project))
        projection_bytes = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        public_bytes = tuple(path.read_bytes() for path in public_paths)
        assert tuple(
            (content, path.stat().st_mtime_ns)
            for path, content in zip(public_paths, public_bytes, strict=True)
        ) == public_identity
        launch = next(
            effect["launch"]
            for effect in value["runs"][0]["effects"]
            if effect["effect_id"] == effect_id
        )
        if expected_projection is None:
            expected_projection = projection_bytes
            expected_terminal_ref = launch["terminal"]["terminal_ref"]
        assert projection_bytes == expected_projection
        assert launch["terminal"]["terminal_ref"] == expected_terminal_ref
        retained_surface = b"\0".join((projection_bytes, *public_bytes))
        retained_values = {
            "projection": value,
            **{
                path.name: json.loads(content)
                for path, content in zip(public_paths, public_bytes, strict=True)
            },
        }
        retained_strings = tuple(_projected_strings(retained_values))
        string_values = {item for _path, item in retained_strings}
        output_hash = hashlib.sha256(f"stdout-{index}".encode()).hexdigest()
        stderr_hash = hashlib.sha256(f"stderr-{index}".encode()).hexdigest()
        for candidate in (
            credential_digest,
            runner_digest,
            private_launch_ref,
            request_digest,
            output_hash,
            stderr_hash,
        ):
            assert candidate not in string_values
            assert candidate.encode() not in retained_surface
            rehash = hashlib.sha256(candidate.encode()).hexdigest()
            assert rehash not in string_values
            assert rehash.encode() not in retained_surface

    assert expected_projection is not None
    assert expected_terminal_ref == terminal["terminal_ref"]
    for pid in range(1, 65_536):
        private_started = json.dumps(
            {"schema": "lockstep.codex-started/v1", "pid": pid, "pgid": pid},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        private_hash = hashlib.sha256(private_started).hexdigest()
        assert str(pid) not in string_values
        assert private_hash.encode() not in retained_surface
    public_launch_paths = {
        path for path, value in retained_strings if value == public_launch_ref
    }
    assert public_launch_paths == {
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "public_launch_ref",
        ),
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "spawn",
            "public_launch_ref",
        ),
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "terminal",
            "public_launch_ref",
        ),
        ("spawn-fence.json", "public_launch_ref"),
        ("public-start.json", "public_launch_ref"),
        ("public-terminal.json", "public_launch_ref"),
    }
    start_paths = {path for path, value in retained_strings if value == start_ref}
    assert start_paths == {
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "spawn",
            "start_ref",
        ),
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "terminal",
            "start_ref",
        ),
        ("spawn-fence.json", "start_ref"),
        ("public-start.json", "start_ref"),
        ("public-terminal.json", "start_ref"),
    }
    terminal_ref_paths = {
        path for path, value in retained_strings if value == terminal["terminal_ref"]
    }
    assert terminal_ref_paths == {
        (
            "projection",
            "runs",
            0,
            "effects",
            0,
            "launch",
            "terminal",
            "terminal_ref",
        ),
        ("public-terminal.json", "terminal_ref"),
    }
    assert b"lockstep-public-terminal-v1\0" not in retained_surface
    assert b"lockstep-public-managed-argv-v1\0" not in retained_surface


def test_execution_evidence_normalizes_argv_and_recovers_public_receipt_truth(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(state, project)
    effect_id = "effect-managed"
    request_digest = "9" * 64
    runner_digest = "0" * 64
    workspace_ref = "workspace:managed"
    private_launch_ref = "codex:" + "7" * 64
    grant_digest = "8" * 64
    public_launch_ref = "c" * 64
    start_ref = "e" * 64
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    executable = "/usr/local/bin/codex"
    argv = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-test",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(workspace),
        "-",
    ]
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        native_commitment = launch_commitment_digest(
            SimpleNamespace(grant_digest=grant_digest),
            PreparedLaunch(
                effect_id,
                request_digest,
                runner_digest,
                private_launch_ref,
                workspace_ref,
            ),
        )
        connection.execute(
            "UPDATE effects SET runner_binding_digest = ?, workspace_ref = ?, "
            "request_digest = ?, grant_digest = ?, launch_commitment_digest = ? "
            "WHERE effect_id = ?",
            (
                runner_digest,
                workspace_ref,
                request_digest,
                grant_digest,
                native_commitment,
                effect_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    attempts = state / "codex-attempts"
    attempts.mkdir(mode=0o700)
    attempt = attempts / hashlib.sha256(effect_id.encode()).hexdigest()
    attempt.mkdir(mode=0o700)
    _write_owner_json(
        attempt / "launch.json",
        {
            "schema": "lockstep.codex-launch/v1",
            "effect_id": effect_id,
            "request_digest": request_digest,
            "runner_binding_digest": runner_digest,
            "workspace_ref": workspace_ref,
            "workspace_path": str(workspace),
            "workspace_purpose": "managed_output",
            "execution_class": "managed-agent",
            "cwd": str(workspace),
            "executable_path": executable,
            "executable_identity_digest": runner_digest,
            "inner_argv": argv,
            "environment": [["SECRET_ENV", "do-not-retain"]],
            "codex_home": str(tmp_path / "secret-codex-home"),
            "credential_identity_digest": "6" * 64,
            "sandbox_policy_digest": "5" * 64,
            "sandbox_attestation_digest": "4" * 64,
            "launcher_decision_generation": 1,
            "deadline_at": "2026-09-01T13:00:00+00:00",
            "launch_ref": private_launch_ref,
            "public_launch_ref": public_launch_ref,
            "start_ref": start_ref,
            "shell": False,
            "close_fds": True,
            "inherited_fds": [],
            "deployment_profile": "local_unsandboxed",
        },
    )
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    prepared = projection.evidence(run_id, str(project))
    launch = next(
        item["launch"]
        for item in prepared["runs"][0]["effects"]
        if item["effect_id"] == effect_id
    )
    normalized = [*argv]
    normalized[13] = "$LOCKSTEP_EFFECT_WORKSPACE"
    assert launch["normalized_argv"] == normalized
    assert launch["normalized_argv_ref"] == hashlib.sha256(
        b"lockstep-public-managed-argv-v1\0"
        + json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert launch["public_launch_ref"] == public_launch_ref
    assert launch["spawn"] is None
    assert launch["terminal"] is None
    assert start_ref not in json.dumps(prepared, sort_keys=True)

    safe_start = {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": effect_id,
        "public_launch_ref": public_launch_ref,
        "start_ref": start_ref,
    }
    _write_owner_document(attempt / "spawn-fence.json", safe_start)
    fenced = projection.evidence(run_id, str(project))
    launch = next(
        item["launch"]
        for item in fenced["runs"][0]["effects"]
        if item["effect_id"] == effect_id
    )
    assert launch["spawn"] == {
        "disposition": "indeterminate",
        "effect_id": effect_id,
        "process_start_count": None,
        "public_launch_ref": public_launch_ref,
        "start_ref": start_ref,
    }

    _write_owner_document(
        attempt / "public-start.json", {**safe_start, "start_ref": "f" * 64}
    )
    invalid_final = projection.evidence(run_id, str(project))
    launch = next(
        item["launch"]
        for item in invalid_final["runs"][0]["effects"]
        if item["effect_id"] == effect_id
    )
    assert launch["spawn"]["disposition"] == "indeterminate"
    assert launch["spawn"]["process_start_count"] is None
    (attempt / "public-start.json").unlink()
    outside_start = tmp_path / "outside-public-start.json"
    _write_owner_document(outside_start, safe_start)
    (attempt / "public-start.json").symlink_to(outside_start)
    symlinked_final = projection.evidence(run_id, str(project))
    launch = next(
        item["launch"]
        for item in symlinked_final["runs"][0]["effects"]
        if item["effect_id"] == effect_id
    )
    assert launch["spawn"]["disposition"] == "indeterminate"
    assert launch["spawn"]["process_start_count"] is None
    (attempt / "public-start.json").unlink()
    _write_owner_document(attempt / "public-start.json", safe_start)
    terminal = {
        "effect_id": effect_id,
        "overflow": False,
        "public_launch_ref": public_launch_ref,
        "quiescent": True,
        "returncode": 0,
        "start_ref": start_ref,
        "termination_reason": "exited",
        "timed_out": False,
    }
    terminal["terminal_ref"] = hashlib.sha256(
        b"lockstep-public-terminal-v1\0"
        + json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_owner_document(attempt / "public-terminal.json", terminal)
    before = _tree_snapshot(state)
    started = projection.evidence(run_id, str(project))
    after = _tree_snapshot(state)
    launch = next(
        item["launch"]
        for item in started["runs"][0]["effects"]
        if item["effect_id"] == effect_id
    )
    assert launch["spawn"] == {
        "disposition": "started",
        "effect_id": effect_id,
        "process_start_count": 1,
        "public_launch_ref": public_launch_ref,
        "start_ref": start_ref,
    }
    assert launch["terminal"] == terminal
    assert before == after
    retained = json.dumps(started, sort_keys=True)
    assert private_launch_ref not in retained
    assert request_digest not in retained
    assert runner_digest not in retained
    assert "do-not-retain" not in retained
    assert "secret-codex-home" not in retained
    assert str(workspace) not in retained

    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "INSERT INTO effect_runtime_inputs "
            "(effect_id, runtime_key, public_run_id, thread_id, checkpoint_ns, "
            "checkpoint_id, task_id, interrupt_id, descriptor_digest, "
            "snapshot_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                effect_id,
                "current_project_snapshot",
                run_id,
                thread_id,
                "",
                "checkpoint-current",
                "task-managed",
                "interrupt-managed",
                "a" * 64,
                "b" * 64,
                "2026-09-01T12:00:00+00:00",
            ),
        )
        connection.execute("DELETE FROM effects WHERE effect_id = ?", (effect_id,))
        connection.commit()
    finally:
        connection.close()
    unmatched = projection.evidence(run_id, str(project))
    assert unmatched["unmatched_launches"] == [
        {
            "disposition": "started",
            "effect_id": effect_id,
            "normalized_argv_ref": launch["normalized_argv_ref"],
            "process_start_count": 1,
            "public_launch_ref": public_launch_ref,
            "start_ref": start_ref,
        }
    ]


def test_execution_evidence_projects_a_two_item_publication_journal(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".lockstep").mkdir()
    recipes = project / "recipes"
    recipes.mkdir()
    state = tmp_path / "owner-state"
    run_id, thread_id = _seed_evidence_effects(state, project)
    definition_digest = "a" * 64
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.native_models import NativeCoordinate
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.publication import (
        ProjectPublisher,
        PublicationEntry,
        PublicationRequest,
    )

    blobs = BlobStore(state)
    snapshots = ProjectSnapshotStore(state, blobs)
    source_files = {"one.md": b"ONE", "two.md": b"TWO"}
    snapshot_ref = snapshots.capture(
        {path: blobs.put(content) for path, content in source_files.items()},
        declared_paths=tuple(source_files),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "6" * 64,
            "workspace_ref": "workspace:native-publication",
        },
    )
    registry = ArtifactRegistry(state, blobs, snapshots)
    artifact_refs = registry.register_set(
        public_run_id=run_id,
        project_identity=str(project.resolve()),
        definition_digest=definition_digest,
        producer_effect_id="effect-managed",
        producer_request_digest="6" * 64,
        workspace_ref="workspace:native-publication",
        producer_coordinate=NativeCoordinate(
            thread_id, "checkpoint-earlier", "", "task-2", "interrupt-2"
        ),
        descriptor_digest="2" * 64,
        snapshot_ref=snapshot_ref,
        declarations=tuple(
            ArtifactDeclaration(name, name, "text/markdown", True)
            for name in source_files
        ),
    )
    publisher = ProjectPublisher(state, project, registry, blobs)
    publisher_binding = publisher.binding_digest
    items: list[dict[str, object]] = []
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        for ordinal, suffix in enumerate(("one", "two")):
            accept_effect = f"effect-accept-{suffix}"
            coordinate = {
                "checkpoint_id": "checkpoint-earlier",
                "checkpoint_ns": "",
                "interrupt_id": f"accept-{suffix}",
                "task_id": f"accept-task-{suffix}",
                "thread_id": thread_id,
            }
            artifact_ref = str(artifact_refs[ordinal])
            artifact_digest = registry.read(artifact_refs[ordinal]).blob.sha256
            destination = f".lockstep/review-{suffix}.md"
            consent_ref = f"consent:{suffix}"
            receipt_digest = ("d" if ordinal == 0 else "e") * 64
            descriptor_digest = ("f" if ordinal == 0 else "9") * 64
            acceptance = {
                "schema": "lockstep.acceptance-result/v1",
                "effect_id": accept_effect,
                "outcome": "PASS",
                "artifact_ref": artifact_ref,
                "artifact_digest": artifact_digest,
                "destination": destination,
                "transformation": "identity",
                "audience": "local-project",
                "consent_ref": consent_ref,
                "approval_generation": 1,
                "receipt_digest": receipt_digest,
            }
            connection.execute(
                "INSERT INTO effects (effect_id, thread_id, checkpoint_ns, "
                "checkpoint_id, task_id, interrupt_id, descriptor_digest, "
                "effect_kind, deadline_at, phase, lease_epoch, created_at, "
                "updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    accept_effect,
                    thread_id,
                    "",
                    "checkpoint-earlier",
                    coordinate["task_id"],
                    coordinate["interrupt_id"],
                    descriptor_digest,
                    "accept",
                    None,
                    "delivered",
                    0,
                    "2026-09-01T12:00:00+00:00",
                    "2026-09-01T12:00:01+00:00",
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO effect_observations "
                "(effect_id, revision, phase, result_json, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    accept_effect,
                    1,
                    "delivered",
                    json.dumps(acceptance, sort_keys=True, separators=(",", ":")),
                    "2026-09-01T12:00:01+00:00",
                ),
            )
            commitment = {
                "schema": "lockstep.publication-consent-commitment/v1",
                "public_run_id": run_id,
                "project_identity": str(project.resolve()),
                "definition_digest": definition_digest,
                "source": coordinate,
                "effect_id": accept_effect,
                "descriptor_digest": descriptor_digest,
                "producer_effect_id": "effect-managed",
                "artifact_ref": artifact_ref,
                "artifact_digest": artifact_digest,
                "destination": destination,
                "transformation": "identity",
                "audience": "local-project",
            }
            commitment["digest"] = hashlib.sha256(
                json.dumps(commitment, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO publication_consents (consent_ref, token_sha256, "
                "project_identity, public_run_id, definition_digest, source_thread_id, "
                "source_checkpoint_ns, source_checkpoint_id, source_task_id, "
                "source_interrupt_id, effect_id, descriptor_digest, producer_effect_id, "
                "artifact_ref, artifact_digest, destination, transformation, audience, "
                "commitment_digest, consent_epoch, issued_at, redeemed_at, receipt_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    consent_ref,
                    ("1" if ordinal == 0 else "2") * 64,
                    str(project.resolve()),
                    run_id,
                    definition_digest,
                    thread_id,
                    "",
                    "checkpoint-earlier",
                    coordinate["task_id"],
                    coordinate["interrupt_id"],
                    accept_effect,
                    descriptor_digest,
                    "effect-managed",
                    artifact_ref,
                    artifact_digest,
                    destination,
                    "identity",
                    "local-project",
                    commitment["digest"],
                    1,
                    "2026-09-01T12:00:00+00:00",
                    "2026-09-01T12:00:01+00:00",
                    receipt_digest,
                ),
            )
            items.append(
                {
                    "ordinal": ordinal,
                    "consent_ref": consent_ref,
                    "acceptance_receipt_digest": receipt_digest,
                    "commitment": commitment,
                }
            )
        consent_set = "consent-set:" + hashlib.sha256(
            json.dumps([item["consent_ref"] for item in items], separators=(",", ":")).encode()
        ).hexdigest()
        request = PublicationRequest.build(
            effect_id="effect-publish",
            public_run_id=run_id,
            project_identity=str(project.resolve()),
            definition_digest=definition_digest,
            coordinate=NativeCoordinate(
                thread_id, "checkpoint-earlier", "", "task-6", "interrupt-6"
            ),
            descriptor_digest="7" * 64,
            authority_request_digest="3" * 64,
            grant_digest="4" * 64,
            publisher_binding_digest=publisher_binding,
            consent_ref=consent_set,
            approval_generation=1,
            policy_epoch=1,
            config_epoch=1,
            parent_capability_generation=1,
            entries=tuple(
                PublicationEntry(
                    artifact_ref=item["commitment"]["artifact_ref"],
                    destination=item["commitment"]["destination"],
                )
                for item in items
            ),
        )
        handle = publisher.prepare(request)
        journal_digest = handle.journal_digest
        connection.execute(
            "UPDATE effects SET phase = 'delivered', request_digest = ?, "
            "runner_binding_digest = ?, launch_commitment_digest = ?, result_ref = ? "
            "WHERE effect_id = 'effect-publish'",
            (
                "3" * 64,
                publisher_binding,
                publisher.commitment_digest(handle),
                f"publication:{journal_digest}",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    projection = Engine.observe(state, recipes)
    app = _EvidenceNativeApp(thread_id)

    @contextmanager
    def native_app(_binding):
        yield app

    projection._resources.native_app = native_app
    value = projection.evidence(run_id, str(project))
    publication = next(
        item["publication"]
        for item in value["runs"][0]["effects"]
        if item["effect_id"] == "effect-publish"
    )
    assert publication == {
        "effect_id": "effect-publish",
        "items": items,
        "journal_digest": journal_digest,
        "phase": "prepared",
    }
    journal_path = publisher.journal_path(handle)
    original_journal = json.loads(journal_path.read_bytes())
    malformed = []
    empty = {**original_journal, "plan": [], "cursor": 0}
    malformed.append(empty)
    malformed.append({**original_journal, "plan": original_journal["plan"][:1]})
    malformed.append(
        {**original_journal, "plan": [*original_journal["plan"], original_journal["plan"][0]]}
    )
    malformed.append({**original_journal, "plan": list(reversed(original_journal["plan"]))})
    malformed.append({**original_journal, "cursor": len(original_journal["plan"]) + 1})
    malformed.append({**original_journal, "phase": "applied", "cursor": 2})
    for candidate in malformed:
        journal_path.write_bytes(
            json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
        )
        with pytest.raises(LockstepError, match="trusted native state"):
            projection.evidence(run_id, str(project))
    journal_path.write_bytes(
        json.dumps(original_journal, sort_keys=True, separators=(",", ":")).encode()
    )


def test_public_engine_has_no_implicit_active_constructor(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Engine(tmp_path / "owner-state", tmp_path / "recipes")


def test_projection_rejects_insecure_existing_owner_root_without_database(
    tmp_path: Path,
) -> None:
    state = tmp_path / "owner-state"
    state.mkdir(mode=0o755)
    recipes = tmp_path / "recipes"
    recipes.mkdir()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).list_runs(str(tmp_path))


def test_projection_rejects_symlinked_owner_root_before_canonicalization(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual-owner-state"
    actual.mkdir(mode=0o700)
    state = tmp_path / "owner-state"
    state.symlink_to(actual, target_is_directory=True)
    recipes = tmp_path / "recipes"
    recipes.mkdir()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).list_runs(str(tmp_path))


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_read_resources_accepts_optional_sqlite_sidecar_disappearing_at_lstat(
    suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "owner-state"
    database = state / "runtime.sqlite"
    store = SQLiteStore(database)
    binding = RunBinding(
        "sidecar-race-" + "a" * 32,
        "thread-sidecar-race",
        "b" * 64,
        "snapshot:" + "c" * 64,
        str(tmp_path.resolve()),
        "2026-09-01T12:00:00+00:00",
    )
    RunCatalog(store).create(binding)
    store.close()
    state_before = _tree_snapshot(state)
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"transient sqlite sidecar")
    sidecar.chmod(0o600)
    real_verify_owner_file = read_resources.verify_owner_file
    disappeared = False

    def verify_after_sqlite_removes_sidecar(path: Path) -> None:
        nonlocal disappeared
        if path == sidecar and not disappeared:
            sidecar.unlink()
            disappeared = True
        real_verify_owner_file(path)

    monkeypatch.setattr(
        read_resources, "verify_owner_file", verify_after_sqlite_removes_sidecar
    )

    assert RuntimeReadResources(state).bindings() == (binding,)
    assert disappeared is True
    assert _tree_snapshot(state) == state_before


@pytest.mark.parametrize("poison", ["symlink", "shared-mode", "nonregular"])
def test_read_resources_rejects_present_poisoned_sqlite_sidecar(
    poison: str,
    tmp_path: Path,
) -> None:
    state = tmp_path / "owner-state"
    database = state / "runtime.sqlite"
    store = SQLiteStore(database)
    store.close()
    sidecar = Path(f"{database}-journal")
    if poison == "symlink":
        outside = tmp_path / "outside-sidecar"
        outside.write_bytes(b"outside")
        sidecar.symlink_to(outside)
    elif poison == "shared-mode":
        sidecar.write_bytes(b"insecure")
        sidecar.chmod(0o604)
    else:
        sidecar.mkdir(mode=0o700)

    with pytest.raises(InsecureStatePath):
        RuntimeReadResources(state).bindings()


def test_read_resources_requires_primary_database_at_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime.sqlite"
    database.write_bytes(b"primary")
    database.chmod(0o600)
    real_verify_owner_file = read_resources.verify_owner_file

    def verify_after_primary_disappears(path: Path) -> None:
        if path == database:
            database.unlink()
        real_verify_owner_file(path)

    monkeypatch.setattr(
        read_resources, "verify_owner_file", verify_after_primary_disappears
    )

    with pytest.raises(FileNotFoundError):
        read_resources._verify_sqlite_family(database)


def test_projection_status_does_not_parse_an_unrelated_thread_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "INSERT INTO effects ("
            "effect_id, thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "interrupt_id, descriptor_digest, effect_kind, deadline_at, phase, "
            "lease_epoch, created_at, updated_at, revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "unrelated-effect",
                "unrelated-thread",
                "",
                "checkpoint",
                "task",
                "interrupt",
                "0" * 64,
                "manual",
                "not-an-iso-timestamp",
                "pending",
                0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert Engine.observe(state, recipes).status(run_id, str(project))["run_id"] == run_id


def test_projection_events_fail_closed_on_relevant_malformed_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    command = LockstepCommandService(state, recipes)
    try:
        run_id = command.start("native-parent-direct", {}, str(project))["run_id"]
    finally:
        command.close()
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "UPDATE effects SET updated_at = ? WHERE thread_id = "
            "(SELECT thread_id FROM runs WHERE public_run_id = ?)",
            ("not-an-iso-timestamp", run_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).events(run_id, str(project))


def test_projection_rejects_catalog_recipe_digest_not_backed_by_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "UPDATE runs SET recipe_digest = ? WHERE public_run_id = ?",
            ("f" * 64, run_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).status(run_id, str(project))


@pytest.mark.parametrize("poison", ["symlink", "shared-mode", "oversize"])
def test_projection_rejects_poisoned_session_binding(
    poison: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    command = LockstepCommandService(state, recipes)
    try:
        run_id = command.start("native-parent-direct", {}, str(project))["run_id"]
        _stop_pump(command)
    finally:
        command.close()
    bindings = state / "bindings"
    bindings.mkdir(mode=0o700, exist_ok=True)
    binding = bindings / f"{run_id}.json"
    if poison == "symlink":
        outside = tmp_path / "outside-binding.json"
        outside.write_text(
            '{"session_id":"attacker","last_seen":"9999-01-01T00:00:00+00:00"}',
            encoding="utf-8",
        )
        binding.symlink_to(outside)
    elif poison == "shared-mode":
        binding.write_text(
            '{"session_id":"attacker","last_seen":"9999-01-01T00:00:00+00:00"}',
            encoding="utf-8",
        )
        binding.chmod(0o604)
    else:
        binding.write_bytes(b"{" + b" " * (64 * 1024))
        binding.chmod(0o600)

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).status(run_id, str(project))


def test_projection_ignores_poisoned_owner_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    expected = Engine.observe(state, recipes).status(run_id, str(project))
    runtime_owner = state / "runtime-owner"
    runtime_owner.mkdir(mode=0o700)
    outside = tmp_path / "poisoned-snapshot.json"
    outside.write_text("not trusted owner state", encoding="utf-8")
    (runtime_owner / "snapshot.json").symlink_to(outside)

    assert Engine.observe(state, recipes).status(run_id, str(project)) == expected


def test_projection_status_preserves_owner_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    before = _tree_snapshot(state)

    result = Engine.observe(state, recipes).status(run_id, str(project))

    assert result["run_id"] == run_id
    assert _tree_snapshot(state) == before


@pytest.mark.parametrize(
    "operation", ["status", "wait", "history", "events", "evidence"]
)
def test_cold_cli_observations_do_not_drive_unrelated_run(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read verb selects projection before any active runtime is constructed."""

    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    before = _tree_snapshot(state)
    argv = ["scenario", operation, run_id]
    if operation == "wait":
        argv.extend(["--timeout", "1"])

    assert cli.main(argv) == 0
    after = _tree_snapshot(state)
    assert after == before, (
        f"cold CLI {operation} mutated unrelated recoverable state: "
        f"{_changed_paths(before, after)}"
    )


@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        (
            "status",
            lambda project, run_id: server.scenario_status(
                run_id, ctx=_context(project)
            ),
        ),
        (
            "wait",
            lambda project, run_id: server.scenario_wait(
                run_id, timeout_seconds=1, ctx=_context(project)
            ),
        ),
        (
            "history",
            lambda project, run_id: server.scenario_history(
                run_id, ctx=_context(project)
            ),
        ),
        (
            "events",
            lambda project, run_id: server.scenario_events(
                run_id, ctx=_context(project)
            ),
        ),
        (
            "evidence",
            lambda project, run_id: server.scenario_evidence(
                run_id, ctx=_context(project)
            ),
        ),
        ("list", lambda project, _run_id: server.list_runs(ctx=_context(project))),
        (
            "trace",
            lambda project, run_id: server.run_trace(run_id, ctx=_context(project)),
        ),
    ],
)
def test_cold_mcp_observations_do_not_construct_driver(
    operation: str,
    invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every MCP read uses the projection handle and leaves command state absent."""

    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    server._reset_engine()
    before = _tree_snapshot(state)
    try:
        invoke(project, run_id)
        after = _tree_snapshot(state)
        active = getattr(server, "_command", None)
        assert {
            "operation": operation,
            "facts_unchanged": after == before,
            "changed_paths": _changed_paths(before, after),
            "active_command_singleton": active is not None,
            "active_command_parts": ()
            if active is None
            else tuple(
                sorted(
                    name
                    for name in (
                        "manual",
                        "coordinator",
                        "authority",
                        "runners",
                        "_pump_thread",
                        "_pump_failure",
                    )
                    if hasattr(active, name)
                )
            ),
        } == {
            "operation": operation,
            "facts_unchanged": True,
            "changed_paths": (),
            "active_command_singleton": False,
            "active_command_parts": (),
        }
    finally:
        server._reset_engine()
