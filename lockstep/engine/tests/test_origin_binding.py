from pathlib import Path

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.recipe_bundles import RecipeBundleStore

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def test_native_child_has_coordinate_but_no_public_run_or_credential(tmp_path):
    store = RecipeBundleStore(tmp_path / "owner")
    materialized = (
        StrictRecipeIngress(FIXTURES)
        .inspect("parent_direct.recipe.yaml")
        .authorize(RecipeAuthorityPolicy())
        .capture(store)
        .materialize(store)
    )
    app = yg.open_native_app(materialized)
    snapshot = app.invoke({}, thread_id="parent")
    app.close()
    child = snapshot.pending[0].coordinate
    assert child.checkpoint_ns
    assert child.task_id and child.interrupt_id
    assert "nonce" not in snapshot.values
    assert "child_run_id" not in snapshot.values
