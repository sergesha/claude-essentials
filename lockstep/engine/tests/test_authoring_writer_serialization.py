"""All automatic writers share one project namespace and kernel lock."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher_module
from lockstep import authoring
from lockstep.authoring_compilation import plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.template_installation import plan_template_installation
from tests._authoring_gate import replace_marker, write_workflow
from tests.test_authoring_legacy_v4_refusal import (
    _create_test_namespace,
    _locate_test_namespace,
)


def _compilation(tmp_path: Path):
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    source = write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state)
    replace_marker(source, "initial", "changed")
    plan = plan_project_compilation(authoring.project_paths(project, "release"))
    return project, state, source, plan


def _template(project: Path, template: str):
    import lockstep.templates as templates
    manifest = templates._manifest(template)
    sources = templates._captured_role_sources(template, "change", manifest)
    return plan_template_installation(project, sources, root_role="change").plan


def _run(*operations):
    barrier = threading.Barrier(len(operations)); results = []
    def invoke(operation):
        try: barrier.wait(10); results.append(operation())
        except BaseException as exc: results.append(exc)
    threads = [threading.Thread(target=invoke, args=(operation,)) for operation in operations]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not thread.is_alive() for thread in threads)
    return results


def _assert_exact(plan) -> None:
    for target in plan.targets:
        assert target.path.read_bytes() == target.after


def _one_lock(state: Path, project: Path) -> Path:
    namespace, _identity = _locate_test_namespace(state, project)
    locks = tuple((state / "authoring").rglob("transaction.lock"))
    assert locks == (namespace / "transaction.lock",)
    return locks[0]


def test_overlapping_replacement_writers_leave_one_complete_namespace(tmp_path) -> None:
    project, state, _source, plan = _compilation(tmp_path); publisher = AuthoringPublisher(state)
    results = _run(lambda: publisher.publish(plan), lambda: AuthoringPublisher(state).publish(plan))
    assert sum(value is None for value in results) == 1
    assert sum(isinstance(value, Exception) for value in results) == 1
    _assert_exact(plan); assert _one_lock(state, project).is_file()


def test_overlapping_distinguishable_templates_leave_one_complete_namespace(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    reviewed, parallel = _template(project, "reviewed-change"), _template(project, "parallel-review")
    results = _run(lambda: AuthoringPublisher(state).publish(reviewed), lambda: AuthoringPublisher(state).publish(parallel))
    assert sum(value is None for value in results) == 1
    assert sum(isinstance(value, Exception) for value in results) == 1
    winner = reviewed if all(target.path.exists() and target.path.read_bytes() == target.after for target in reviewed.targets) else parallel
    _assert_exact(winner); assert _one_lock(state, project).is_file()


def test_disjoint_replacement_and_template_writers_serialize_under_one_lock(tmp_path) -> None:
    project, state, _source, compilation = _compilation(tmp_path); template = _template(project, "reviewed-change")
    results = _run(lambda: AuthoringPublisher(state).publish(compilation), lambda: AuthoringPublisher(state).publish(template))
    assert results == [None, None] or results == [None, None]
    _assert_exact(compilation); _assert_exact(template); assert _one_lock(state, project).is_file()


def test_queued_writer_revalidates_sources_after_lock_acquisition(tmp_path) -> None:
    project, state, source, plan = _compilation(tmp_path); namespace, _identity = _create_test_namespace(state, project)
    result = []; started = threading.Event()
    def queued():
        started.set()
        try: AuthoringPublisher(state).publish(plan); result.append(None)
        except BaseException as exc: result.append(exc)
    with advisory_file_lock(namespace / "transaction.lock"):
        thread = threading.Thread(target=queued); thread.start(); assert started.wait(5)
        source.write_bytes(b"foreign source\n")
    thread.join(10)
    assert len(result) == 1 and isinstance(result[0], Exception)
    for target in plan.targets:
        assert target.path.read_bytes() == target.before


def test_process_death_releases_lock_for_next_writer(tmp_path, monkeypatch) -> None:
    project, state, _source, plan = _compilation(tmp_path); original = publisher_module._publish_per_file; calls = 0
    class Death(BaseException): pass
    def die_once(value):
        nonlocal calls; calls += 1
        if calls == 1: raise Death("cut")
        return original(value)
    monkeypatch.setattr(publisher_module, "_publish_per_file", die_once)
    with pytest.raises(Death): AuthoringPublisher(state).publish(plan)
    AuthoringPublisher(state).publish(plan)
    _assert_exact(plan); assert calls == 2 and _one_lock(state, project).is_file()
