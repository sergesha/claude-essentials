from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.recipe.authority import (
    AuthorityDenied,
    OwnerReviewedGrant,
    OwnerReviewedPythonTarget,
    RecipeAuthorityPolicy,
    RecipeLimits,
    StrictRecipeIngress,
)


@pytest.fixture(autouse=True)
def _restore_materialization_permissions(tmp_path):
    yield
    for path in sorted(
        tmp_path.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _safe_recipe(*, name: str = "root", extra: str = "") -> str:
    return f"""\
version: "1.0"
name: {name}
state: {{phase: str}}
nodes:
  wait:
    type: interrupt
    message: {{step: wait}}
    state_key: brief
    resume_key: evidence
    idempotent: false
edges:
  - {{from: START, to: wait}}
  - {{from: wait, to: END}}
{extra}"""


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            """name: first\nname: second\nnodes: {}\nedges: []\n""",
            "duplicate mapping key",
        ),
        (
            """name: root\nnodes: &nodes {}\ncopy: *nodes\nedges: []\n""",
            "anchors are forbidden",
        ),
        (
            """name: root\nnodes: {}\nedges: []\nenabled: yes\n""",
            "ambiguous scalar",
        ),
        (
            """name: root\nnodes: {}\nedges: []\ncreated: 2026-08-20\n""",
            "ambiguous scalar",
        ),
    ],
)
def test_strict_ingress_rejects_yaml_ambiguity_before_admission(
    tmp_path, yaml_text, message
):
    root = tmp_path / "root.recipe.yaml"
    _write(root, yaml_text)

    with pytest.raises(ValueError, match=message):
        StrictRecipeIngress(tmp_path).inspect(root.name)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("unknown_top_level: true\n", "unknown top-level field"),
        ("data_files: {payload: payload.yaml}\n", "data_files"),
        ("prompts_dir: prompts\n", "prompts_dir"),
        ("checkpointer: {type: sqlite}\n", "checkpointer"),
        (
            "tools:\n  helper:\n    manifest: tool.yaml\n",
            "manifest",
        ),
        (
            "tools:\n  child:\n    type: graph\n    path: child.yaml\n",
            "graph tool",
        ),
    ],
)
def test_strict_ingress_fails_closed_for_unprofiled_loader_inputs(
    tmp_path, extra, message
):
    root = tmp_path / "root.recipe.yaml"
    _write(root, _safe_recipe(extra=extra))

    with pytest.raises(ValueError, match=message):
        StrictRecipeIngress(tmp_path).inspect(root.name)


def test_ingress_closes_recursive_subgraph_dag_and_canonicalizes_every_file(tmp_path):
    root = tmp_path / "root.recipe.yaml"
    child = tmp_path / "flows" / "child.yaml"
    grandchild = tmp_path / "shared" / "grandchild.yaml"
    _write(
        root,
        """\
name: root
state: {phase: str}
nodes:
  child: {type: subgraph, graph: flows/child.yaml, mode: direct}
edges: [{from: START, to: child}, {from: child, to: END}]
""",
    )
    _write(
        child,
        """\
name: child
state: {phase: str}
nodes:
  nested: {type: subgraph, graph: ../shared/grandchild.yaml, mode: direct}
edges: [{from: START, to: nested}, {from: nested, to: END}]
""",
    )
    _write(grandchild, _safe_recipe(name="grandchild"))

    candidate = StrictRecipeIngress(tmp_path).inspect(root.name)

    assert candidate.dependency_dag.root == "root.recipe.yaml"
    assert candidate.dependency_dag.files == (
        "flows/child.yaml",
        "root.recipe.yaml",
        "shared/grandchild.yaml",
    )
    assert all(file.bytes.startswith(b'{"') for file in candidate.files)
    assert candidate.files[0].path == "flows/child.yaml"
    assert len(candidate.definition_sha256) == 64


def test_shared_subgraph_dependency_is_a_dag_not_a_false_cycle(tmp_path):
    root = tmp_path / "root.recipe.yaml"
    left = tmp_path / "left.yaml"
    right = tmp_path / "right.yaml"
    leaf = tmp_path / "leaf.yaml"
    _write(
        root,
        """\
name: root
nodes:
  left: {type: subgraph, graph: left.yaml, mode: direct}
  right: {type: subgraph, graph: right.yaml, mode: direct}
edges: [{from: START, to: left}, {from: left, to: right}, {from: right, to: END}]
""",
    )
    for path, name in ((left, "left"), (right, "right")):
        _write(
            path,
            f"""\
name: {name}
nodes:
  leaf: {{type: subgraph, graph: leaf.yaml, mode: direct}}
edges: [{{from: START, to: leaf}}, {{from: leaf, to: END}}]
""",
        )
    _write(leaf, _safe_recipe(name="leaf"))

    candidate = StrictRecipeIngress(tmp_path).inspect(root.name)

    assert candidate.dependency_dag.files == (
        "leaf.yaml",
        "left.yaml",
        "right.yaml",
        "root.recipe.yaml",
    )


def test_recursive_subgraphs_are_rejected_as_not_a_dependency_dag(tmp_path):
    root = tmp_path / "root.recipe.yaml"
    child = tmp_path / "child.yaml"
    _write(
        root,
        """\
name: root
nodes: {child: {type: subgraph, graph: child.yaml, mode: direct}}
edges: [{from: START, to: child}, {from: child, to: END}]
""",
    )
    _write(
        child,
        """\
name: child
nodes: {root: {type: subgraph, graph: root.recipe.yaml, mode: direct}}
edges: [{from: START, to: root}, {from: root, to: END}]
""",
    )

    with pytest.raises(ValueError, match="not a DAG"):
        StrictRecipeIngress(tmp_path).inspect(root.name)


@pytest.mark.parametrize("graph_ref", ["/outside.yaml", "../outside.yaml"])
def test_subgraph_dependency_must_remain_inside_source_root(tmp_path, graph_ref):
    root = tmp_path / "root.recipe.yaml"
    _write(
        root,
        f"""\
name: root
nodes:
  child: {{type: subgraph, graph: {graph_ref}, mode: direct}}
edges: [{{from: START, to: child}}, {{from: child, to: END}}]
""",
    )

    with pytest.raises(ValueError, match="subgraph path"):
        StrictRecipeIngress(tmp_path).inspect(root.name)


@pytest.mark.parametrize(
    ("recipe", "message"),
    [
        (
            "name: root\nnodes: {x: {type: llm, prompt: p}}\nedges: []\n",
            "unsupported node kind",
        ),
        (
            "name: root\nnodes: {x: {type: interrupt, prompt: p}}\nedges: []\n",
            "unknown field",
        ),
        (
            "name: root\nnodes: {}\nedges: [{from: START, to: END, command: x}]\n",
            "edge 0 has unknown field",
        ),
        (
            "name: root\nnodes: {}\nedges: [{from: START}]\n",
            "edge 0 to",
        ),
        (
            "name: root\nstate: {phase: mystery}\nnodes: {}\nedges: []\n",
            "state field",
        ),
        (
            """\
name: root
tools:
  code: {type: python, path: code.py, function: run}
nodes: {code: {type: python, tool: code}}
edges: [{from: START, to: code}, {from: code, to: END}]
""",
            "python tool 'code' has unknown field",
        ),
        (
            """\
name: root
tools:
  command: {type: shell, command: "printf ok", env: [HOME]}
nodes: {command: {type: tool, tool: command}}
edges: [{from: START, to: command}, {from: command, to: END}]
""",
            "shell tool 'command' env",
        ),
        (
            """\
name: root
tools:
  command: {type: shell, command: "printf ok", timeout: false}
nodes: {command: {type: tool, tool: command}}
edges: [{from: START, to: command}, {from: command, to: END}]
""",
            "shell tool 'command' timeout",
        ),
        (
            """\
name: root
tools:
  command: {type: shell, command: "printf ok", parse: yaml}
nodes: {command: {type: tool, tool: command}}
edges: [{from: START, to: command}, {from: command, to: END}]
""",
            "shell tool 'command' parse",
        ),
        (
            """\
name: root
nodes:
  wait: {type: interrupt, message: {}, idempotent: 1}
edges: [{from: START, to: wait}, {from: wait, to: END}]
""",
            "node 'wait' idempotent",
        ),
        (
            """\
name: root
nodes:
  child: {type: subgraph, graph: child.yaml, mode: fork}
edges: [{from: START, to: child}, {from: child, to: END}]
""",
            "node 'child' mode",
        ),
        (
            """\
name: root
nodes:
  wait: {type: interrupt, message: {step: wait}}
edges: [{from: START, to: wait}, {from: wait, to: END}]
config: {recursion_limit: false}
""",
            "config recursion_limit",
        ),
    ],
)
def test_closed_profile_rejects_unknown_or_malformed_authority_fields(
    tmp_path, recipe, message
):
    root = tmp_path / "root.recipe.yaml"
    _write(root, recipe)

    with pytest.raises(ValueError, match=message):
        StrictRecipeIngress(tmp_path).inspect(root.name)


def test_executable_recipe_requires_exact_owner_reviewed_full_os_grant(tmp_path):
    root = tmp_path / "root.recipe.yaml"
    _write(
        root,
        """\
name: root
state: {evidence: dict}
tools:
  validate:
    type: python
    module: lockstep.runtime.validators
    function: run_checks
nodes:
  validate: {type: python, tool: validate}
edges: [{from: START, to: validate}, {from: validate, to: END}]
""",
    )
    candidate = StrictRecipeIngress(tmp_path).inspect(root.name)
    requirement = candidate.authority_requirements[0]

    with pytest.raises(TypeError, match="RecipeAuthorityPolicy"):
        candidate.authorize(object())  # type: ignore[arg-type]

    with pytest.raises(AuthorityDenied, match="os_user_execution"):
        candidate.authorize(RecipeAuthorityPolicy())

    wrong_recipe = OwnerReviewedGrant(
        recipe_sha256="0" * 64,
        requirement_sha256=requirement.sha256,
        authority="os_user_execution",
    )
    with pytest.raises(AuthorityDenied):
        candidate.authorize(RecipeAuthorityPolicy((wrong_recipe,)))

    grant = OwnerReviewedGrant(
        recipe_sha256=candidate.definition_sha256,
        requirement_sha256=requirement.sha256,
        authority="os_user_execution",
    )
    with pytest.raises(AuthorityDenied, match="exact owner-reviewed"):
        candidate.authorize(RecipeAuthorityPolicy((grant,)))

    wrong_target = OwnerReviewedPythonTarget(
        module="lockstep.runtime.validators",
        function="different_function",
    )
    with pytest.raises(AuthorityDenied):
        candidate.authorize(
            RecipeAuthorityPolicy((grant,), python_targets=(wrong_target,))
        )

    target = OwnerReviewedPythonTarget(
        module="lockstep.runtime.validators",
        function="run_checks",
    )
    admitted = candidate.authorize(
        RecipeAuthorityPolicy((grant,), python_targets=(target,))
    )

    assert admitted.definition_sha256 == candidate.definition_sha256
    assert requirement.uses == ("root.recipe.yaml#/nodes/validate",)


def test_recipe_change_invalidates_executable_grant(tmp_path):
    root = tmp_path / "root.recipe.yaml"
    _write(
        root,
        """\
name: root
tools:
  command:
    type: shell
    command: "printf reviewed"
nodes:
  command: {type: tool, tool: command}
edges: [{from: START, to: command}, {from: command, to: END}]
""",
    )
    ingress = StrictRecipeIngress(tmp_path)
    first = ingress.inspect(root.name)
    req = first.authority_requirements[0]
    grant = OwnerReviewedGrant(
        recipe_sha256=first.definition_sha256,
        requirement_sha256=req.sha256,
        authority="os_user_execution",
    )
    first.authorize(RecipeAuthorityPolicy((grant,)))

    _write(root, root.read_text().replace("reviewed", "changed"))
    changed = ingress.inspect(root.name)

    with pytest.raises(AuthorityDenied):
        changed.authorize(RecipeAuthorityPolicy((grant,)))


def test_authorized_recipe_captures_only_canonical_validated_dag(tmp_path):
    from lockstep.runtime.recipe_bundles import RecipeBundleStore

    source = tmp_path / "source"
    root = source / "root.recipe.yaml"
    child = source / "child.yaml"
    ignored = source / "ignored.yaml"
    _write(
        root,
        """\
name: root
nodes: {child: {type: subgraph, graph: child.yaml, mode: direct}}
edges: [{from: START, to: child}, {from: child, to: END}]
""",
    )
    _write(child, _safe_recipe(name="child"))
    _write(ignored, "secret: not-in-the-DAG\n")
    store = RecipeBundleStore(tmp_path / "owner-state")
    authorized = (
        StrictRecipeIngress(source)
        .inspect(root.name)
        .authorize(RecipeAuthorityPolicy())
    )

    admitted = authorized.capture(store)
    root.write_text("changed after admission")
    child.unlink()
    materialized = admitted.materialize(store)

    assert materialized.source_path.read_bytes().startswith(b'{"')
    assert (materialized.directory / "child.yaml").read_bytes().startswith(b'{"')
    assert not (materialized.directory / "ignored.yaml").exists()
    assert materialized.definition_sha256 == authorized.definition_sha256


def test_rejected_recipe_does_not_publish_a_bundle(tmp_path):
    from lockstep.runtime.recipe_bundles import RecipeBundleStore

    source = tmp_path / "source"
    root = source / "root.recipe.yaml"
    _write(root, "name: one\nname: two\nnodes: {}\nedges: []\n")
    RecipeBundleStore(tmp_path / "owner-state")

    with pytest.raises(ValueError, match="duplicate"):
        StrictRecipeIngress(source).inspect(root.name)

    assert list((tmp_path / "owner-state" / "recipe-bundles").iterdir()) == []


def test_native_adapter_compiles_only_authorized_immutable_materialization(tmp_path):
    import lockstep.recipe.yamlgraph_adapter as yg
    from lockstep.runtime.recipe_bundles import RecipeBundleStore

    source = tmp_path / "source"
    root = source / "root.recipe.yaml"
    _write(root, _safe_recipe())
    store = RecipeBundleStore(tmp_path / "owner-state")
    materialized = (
        StrictRecipeIngress(source)
        .inspect(root.name)
        .authorize(RecipeAuthorityPolicy())
        .capture(store)
        .materialize(store)
    )

    with pytest.raises(TypeError, match="AuthorizedMaterialization"):
        yg.open_native_app(root)

    assert yg.validate_native(materialized) == (True, "ok")
    app = yg.open_native_app(materialized)
    parked = app.invoke({}, thread_id="authorized")
    app.close()

    assert [item.value for item in parked.pending] == [{"step": "wait"}]


def test_raw_path_adapter_surface_is_removed_after_native_cutover():
    import lockstep.recipe.yamlgraph_adapter as yg

    assert not hasattr(yg, "compile_recipe")
    assert not hasattr(yg, "validate")
    assert not hasattr(yg, "legacy_compile_recipe")
    assert not hasattr(yg, "legacy_validate_recipe")
    assert not hasattr(yg, "render")
    assert not hasattr(yg, "cli_mermaid")


@pytest.mark.parametrize(
    ("limits", "recipe", "message"),
    [
        (RecipeLimits(max_source_bytes=32), _safe_recipe(), "source bytes"),
        (RecipeLimits(max_depth=2), _safe_recipe(), "depth"),
        (RecipeLimits(max_nodes=4), _safe_recipe(), "nodes"),
        (RecipeLimits(max_container_items=1), _safe_recipe(), "container items"),
        (RecipeLimits(max_scalar_bytes=3), _safe_recipe(), "scalar bytes"),
        (
            RecipeLimits(max_integer_abs=10),
            _safe_recipe(extra="config: {recursion_limit: 11}\n"),
            "integer range",
        ),
    ],
)
def test_ingress_has_hard_decode_budgets(tmp_path, limits, recipe, message):
    root = tmp_path / "root.recipe.yaml"
    _write(root, recipe)

    with pytest.raises(ValueError, match=message):
        StrictRecipeIngress(tmp_path, limits=limits).inspect(root.name)
