"""Presence-only refusal for every retained authoring transaction byte shape."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import lockstep.authoring_publisher as publisher_module
import pytest
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.recipe._authority_models import RecipeCandidate
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from lockstep.templates import install_template

from lockstep import authoring
from tests._authoring_gate import replace_marker, tree_image, write_workflow

FIXTURE = Path(__file__).parent / "fixtures/authoring-v4/transaction.json"
PAYLOADS = (pytest.param(FIXTURE.read_bytes(), id="real-v4"), pytest.param(b"{malformed", id="malformed"), pytest.param(b'{"schema":"unknown/v99"}', id="unknown"), pytest.param(b'{"schema":"lockstep.authoring-transaction/v2"}', id="v2"),
    pytest.param(b'{"schema":"lockstep.authoring-transaction/v3"}', id="v3"))

def live_v4_bytes(project: Path) -> bytes:
    document = json.loads(FIXTURE.read_bytes()); old = document["project"]["path"]
    root = project.resolve(); info = root.stat()
    def bind(value):
        if isinstance(value, dict):
            result = {key: bind(item) for key, item in value.items()}
            if "device" in result and "inode" in result: result.update(device=info.st_dev, inode=info.st_ino)
            return result
        if isinstance(value, list): return [bind(item) for item in value]
        if isinstance(value, str) and value.startswith(old): return str(root) + value[len(old):]
        return value
    return json.dumps(bind(document), sort_keys=True, separators=(",", ":")).encode()
def _create_test_namespace(state: Path, project: Path, *, ready: bool = True):
    namespace, identity = publisher_module._create_authoring_namespace_for_project(state, project)
    if ready:
        with publisher_module._locked_authoring_namespace(namespace, create=True): pass
    return namespace, identity
def _locate_test_namespace(state: Path, project: Path):
    namespace, identity = publisher_module._locate_authoring_namespace(state, project)
    assert namespace is not None
    return namespace, identity
def _project(tmp_path: Path):
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "owner-state").resolve()
    write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state)
    namespace, identity = _locate_test_namespace(state, project)
    digest = hashlib.sha256(json.dumps({"path": str(identity.path), "device": identity.device, "inode": identity.inode}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert identity.path == project.resolve() and namespace == state / "authoring" / digest
    return project, state, namespace
def _retain(namespace: Path, payload: bytes) -> Path:
    transaction = namespace / "transaction.json"; transaction.write_bytes(payload); transaction.chmod(0o600); return transaction
def _guidance(call, project: Path, state: Path) -> None:
    project_before, owner_before = tree_image(project), tree_image(state)
    with pytest.raises(Exception) as raised:
        call()
    message = str(raised.value)
    assert "v4" in message and "pre-simplification" in message
    assert str(project.resolve()) in message and str(state) in message
    assert "Do not delete transaction.json manually" in message
    assert tree_image(project) == project_before and tree_image(state) == owner_before

@pytest.mark.parametrize("payload", PAYLOADS)
def test_present_bytes_are_refused_without_parsing_or_mutation(tmp_path, payload) -> None:
    project, state, namespace = _project(tmp_path); raw = FIXTURE.read_bytes()
    _retain(namespace, live_v4_bytes(project) if payload == raw else payload)
    _guidance(lambda: AuthoringPublisher(state).observe(project, lambda: pytest.fail("operation ran")), project, state)
    assert FIXTURE.read_bytes() == raw

def test_live_v4_blocks_all_planning_and_runtime_admission(tmp_path, monkeypatch) -> None:
    from lockstep import templates
    project, state, namespace = _project(tmp_path); _retain(namespace, live_v4_bytes(project)); replace_marker(project / ".lockstep/workflows/release.workflow.yaml", "initial", "edited")
    reached = []
    def blocked(*_a, **_k): reached.append(True); pytest.fail("planning or admission ran")
    for owner, name in ((authoring, "compile_project"), (authoring, "plan_captured_workflow_installation"),
                        (templates, "plan_template_installation"), (RecipeCandidate, "authorize"), (AuthorizedStartService, "start")):
        monkeypatch.setattr(owner, name, blocked)
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    calls = (lambda: authoring.publish_project_compilation(project, "release", state_dir=state), lambda: authoring.initialize_minimal(project, "other", state_dir=state),
             lambda: install_template("reviewed-change", "change", project, state_dir=state), lambda: service.start("release", {}, str(project)))
    try:
        for call in calls: _guidance(call, project, state)
    finally: service.close()
    assert reached == []

@pytest.mark.parametrize("layout", ("state-in-project", "project-in-state"))
def test_state_and_project_namespaces_must_be_disjoint(tmp_path, layout) -> None:
    project = tmp_path / "project"; state = project / "state"
    if layout == "project-in-state": state = tmp_path / "state"; project = state / "project"
    project.mkdir(parents=True); before = tree_image(tmp_path)
    with pytest.raises(ValueError, match="outside the project"): AuthoringPublisher(state.resolve()).observe(project, lambda: "forbidden")
    assert tree_image(tmp_path) == before
