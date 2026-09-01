"""Runtime start admits exactly one freshly observed canonical recipe DAG."""
from __future__ import annotations

import fcntl, json, os, sys
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher_module
from lockstep import authoring
from lockstep.authoring_publisher import AuthoringPublisher, observe_authoring_project
from lockstep.runtime.engine import LockstepError
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from lockstep.recipe._authority_models import RecipeCandidate
from lockstep.workflow.compiler import canonical_execution_bytes
from lockstep.templates import install_template
from tests._authoring_gate import (
    assert_no_durable_runtime_change,
    mcp_context,
    provision_controlled_runtime,
    tree_image,
    write_workflow,
)
from tests.test_authoring_legacy_v4_refusal import (
    _create_test_namespace,
    _locate_test_namespace,
    _retain,
    live_v4_bytes,
)


def _ready(tmp_path: Path, *, state_name: str = "state") -> tuple[Path, Path]:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / state_name).resolve()
    write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state)
    return project, state


def _start(project: Path, state: Path, root: str = "release"):
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    try: return service.start(root, {}, str(project))
    finally: service.close()


def _stop(captured: list):
    def stop(_self, recipe, plan, _values, *, canonical_input):
        captured.append((recipe, plan, canonical_input)); return {"status": "captured", "run_id": "probe"}
    return stop


def test_public_start_uses_one_locked_canonical_admission(tmp_path, monkeypatch) -> None:
    project, state = _ready(tmp_path); active = False; captured = []; authorizations = []
    original = publisher_module._ExistingAuthoringBoundary.observe
    def observe(self, operation):
        def checked():
            nonlocal active; active = True
            try: return operation()
            finally: active = False
        return original(self, checked)
    original_authorize = RecipeCandidate.authorize
    def authorize(candidate, policy):
        assert active; authorizations.append(candidate); return original_authorize(candidate, policy)
    def stop(*args, **kwargs): return _stop(captured)(*args, **kwargs)
    monkeypatch.setattr(publisher_module._ExistingAuthoringBoundary, "observe", observe)
    monkeypatch.setattr(RecipeCandidate, "authorize", authorize)
    monkeypatch.setattr(AuthorizedStartService, "start", stop)

    assert _start(project, state)["run_id"] == "probe"
    assert len(captured) == 1 and len(authorizations) == 1


def _ready_surface(tmp_path: Path, surface: str):
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    if surface == "compile": write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state); root = "release"
    elif surface == "minimal": authoring.initialize_minimal(project, "release", state_dir=state); root = "release"
    else: install_template("reviewed-change", "change", project, state_dir=state); root = "change"
    return project, state, root


def _assert_dependency_graph(surface: str, authorized) -> None:
    root = "change.recipe.yaml" if surface == "template" else "release.recipe.yaml"
    files = tuple(item.path for item in authorized.files); edges = set()
    assert authorized.root == root and authorized.dependency_dag.root == root and authorized.dependency_dag.files == files
    for item in authorized.files:
        for node in json.loads(item.bytes)["nodes"].values():
            if node.get("type") == "subgraph": edges.add((item.path, node["graph"]))
    if surface != "template": assert files == (root,) and edges == set()
    else:
        generated = next(path for path in files if path.startswith("generated/children/"))
        assert files == ("change-review.recipe.yaml", "change.recipe.yaml", generated)
        assert edges == {("change.recipe.yaml", "change-review.recipe.yaml"), ("change.recipe.yaml", generated)}


@pytest.mark.parametrize("surface", ("compile", "minimal", "template"))
def test_public_start_admits_exact_execution_files_provenance_and_dag(tmp_path, monkeypatch, surface) -> None:
    project, state, root = _ready_surface(tmp_path, surface); captured = []
    if surface == "template":
        provision_controlled_runtime(project, state, root)
    monkeypatch.setattr(AuthorizedStartService, "start", _stop(captured))
    _start(project, state, root)
    _recipe, plan, canonical_input = captured[0]
    recipes = project / ".lockstep/recipes"
    expected = {p.relative_to(recipes).as_posix(): canonical_execution_bytes(p.read_bytes(), logical_path=p.relative_to(recipes).as_posix()) for p in recipes.rglob("*.recipe.yaml")}
    observed = {item.path: item.bytes for item in plan.authorized.files}
    proof = plan.compiler_provenance
    assert observed == expected and canonical_input == b"{}"
    assert proof is not None and proof.context == "canonical-match"
    _assert_dependency_graph(surface, plan.authorized)
    assert {item.relative_path: item.canonical_execution_bytes for item in proof.files} == expected
    assert proof.source_bundle_sha256 == plan.authorized.source_bundle_sha256


@pytest.mark.parametrize("foreign", ("bytes", "symlink", "directory"))
def test_public_start_rejects_noncanonical_before_any_durable_admission(
    tmp_path, monkeypatch, foreign
) -> None:
    project, state = _ready(tmp_path); target = project / ".lockstep/recipes/release.recipe.yaml"
    if foreign == "bytes": target.write_bytes(b"name: foreign\n")
    else:
        target.unlink()
        if foreign == "symlink": target.symlink_to(project / ".lockstep/workflows/release.workflow.yaml")
        else: target.mkdir()
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    reached = []
    monkeypatch.setattr(AuthorizedStartService, "start", lambda *_a, **_k: reached.append(True))
    before = tree_image(state)
    try:
        with pytest.raises((LockstepError, OSError, ValueError)): service.start("release", {}, str(project))
    finally: service.close()
    assert reached == []; assert_no_durable_runtime_change(before, state)


def test_public_start_denies_python_before_import_or_owner_state(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"; project.mkdir(); state = tmp_path / "state"; sentinel = project / "IMPORTED"
    (project / "attacker.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\ndef run(state): return state\n")
    recipes = project / ".lockstep/recipes"; recipes.mkdir(parents=True)
    (recipes / "release.recipe.yaml").write_text("name: release\ntools:\n  code: {type: python, module: attacker, function: run}\nnodes: {code: {type: python, tool: code}}\nedges: [{from: START, to: code}, {from: code, to: END}]\n")
    monkeypatch.syspath_prepend(str(project)); sys.modules.pop("attacker", None)
    with pytest.raises(LockstepError, match="executable authority denied"): _start(project, state)
    assert not state.exists() and "attacker" not in sys.modules and not sentinel.exists()


@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_observer_discards_optimistic_result_when_boundary_appears(tmp_path, outcome) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve(); calls = []
    def operation():
        calls.append(len(calls));
        if len(calls) == 1:
            _create_test_namespace(state, project)
            if outcome == "failure": raise LockstepError("optimistic failure")
        return f"result-{len(calls)}"
    assert observe_authoring_project(state, project, operation) == "result-2"
    assert calls == [0, 1]


def test_observer_uses_one_optimistic_plan_while_boundary_absent(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve(); calls = []
    assert observe_authoring_project(state, project, lambda: calls.append(1) or "ok") == "ok"
    assert calls == [1] and not state.exists()


def test_observer_reraises_original_optimistic_error_without_creating_boundary(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve(); calls = []
    failure = LockstepError("optimistic failure")
    def operation():
        calls.append(1)
        raise failure
    with pytest.raises(LockstepError) as raised:
        observe_authoring_project(state, project, operation)
    assert raised.value is failure and calls == [1] and not state.exists()


def test_unready_boundary_is_read_only_and_never_repaired(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    namespace, _identity = _create_test_namespace(state, project, ready=False); before = tree_image(state)
    with pytest.raises(Exception, match="initialization is incomplete"):
        AuthoringPublisher(state).observe(project, lambda: "forbidden")
    assert tree_image(state) == before and not (namespace / "transaction.lock").exists()


def _require_kernel_lock(state: Path, project: Path) -> None:
    namespace, _identity = _locate_test_namespace(state, project)
    descriptor = os.open(namespace / "transaction.lock", os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError): fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally: os.close(descriptor)


def test_publisher_observe_holds_existing_transaction_lock_through_successful_callback(tmp_path) -> None:
    project, state = _ready(tmp_path); calls = []
    def operation():
        _require_kernel_lock(state, project); calls.append("locked"); return "observed"
    assert AuthoringPublisher(state).observe(project, operation) == "observed"
    assert calls == ["locked"]


@pytest.mark.parametrize(("adapter", "all_names"), (("cli", False), ("mcp", False), ("cli", True)))
def test_named_and_check_all_hold_lock_through_enumeration_and_complete_observation(tmp_path, monkeypatch, capsys, adapter, all_names) -> None:
    from lockstep import cli
    from lockstep.mcp import server
    project, state = _ready(tmp_path); observed = []
    original_check, original_paths, original_glob = authoring.check_recipe, authoring.project_paths, Path.glob
    def check(root, name):
        result = original_check(root, name); _require_kernel_lock(state, project); observed.append(("complete", name)); return result
    def paths(root, name): _require_kernel_lock(state, project); observed.append(("lookup", name)); return original_paths(root, name)
    def glob(path, pattern):
        if path == project / ".lockstep/recipes": _require_kernel_lock(state, project); observed.append(("enumerate", pattern))
        return original_glob(path, pattern)
    monkeypatch.setattr(authoring, "check_recipe", check); monkeypatch.setattr(authoring, "project_paths", paths); monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    if adapter == "cli":
        monkeypatch.chdir(project)
        assert cli.main(["recipe", "check", "--all"] if all_names else ["recipe", "check", "release"]) == 0; capsys.readouterr()
    else: assert server.recipe_check("release", ctx=mcp_context(project))["ok"]
    assert any(kind == "complete" for kind, _name in observed)
    assert any(kind == ("enumerate" if all_names else "lookup") for kind, _name in observed)


def test_legacy_check_all_refuses_before_enumeration(tmp_path, monkeypatch, capsys) -> None:
    from lockstep import cli
    project, state = _ready(tmp_path); namespace, _identity = _locate_test_namespace(state, project)
    _retain(namespace, live_v4_bytes(project)); enumerated = []
    original = Path.glob
    monkeypatch.setattr(Path, "glob", lambda path, pattern: enumerated.append((path, pattern)) or original(path, pattern))
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); monkeypatch.chdir(project); before = tree_image(tmp_path)
    assert cli.main(["recipe", "check", "--all"]) == 2
    assert "pre-simplification" in capsys.readouterr().err and enumerated == [] and tree_image(tmp_path) == before


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
@pytest.mark.parametrize("action", ("check", "diff"))
def test_invalid_check_and_diff_are_state_free_and_write_free(tmp_path, monkeypatch, capsys, adapter, action) -> None:
    from lockstep import cli
    from lockstep.mcp import server
    project = tmp_path / "project"; project.mkdir(); write_workflow(project, "release"); state = tmp_path / "state"; before = tree_image(project); invalid = "../../../escape"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    if adapter == "cli":
        monkeypatch.chdir(project)
        assert cli.main(["recipe", action, invalid]) == 2; assert "invalid workflow name" in capsys.readouterr().err
    else:
        command = server.recipe_check if action == "check" else server.recipe_diff
        with pytest.raises(Exception, match="invalid workflow name"): command(invalid, ctx=mcp_context(project))
    assert not state.exists() and tree_image(project) == before


def test_raw_render_and_estimate_ignore_ambient_authoring_state_deterministically(tmp_path, monkeypatch) -> None:
    project, state = _ready(tmp_path); baseline = (authoring.render_recipe(project, "release", "workflow"), authoring.estimate_recipe(project, "release"))
    namespace, _identity = _locate_test_namespace(state, project); raw_fixture = Path(__file__).parent / "fixtures/authoring-v4/transaction.json"; immutable = raw_fixture.read_bytes()
    _retain(namespace, live_v4_bytes(project)); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); before = tree_image(tmp_path)
    assert tuple((authoring.render_recipe(project, "release", "workflow"), authoring.estimate_recipe(project, "release")) for _ in range(2)) == (baseline, baseline)
    assert tree_image(tmp_path) == before and raw_fixture.read_bytes() == immutable
