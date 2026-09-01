from pathlib import Path


def test_forbidden_legacy_lifecycle_tests_are_removed():
    tests = Path(__file__).parents[1]
    forbidden = {
        "_subcall_helpers.py",
        "test_engine_subcalls.py",
        "test_integration_subcalls.py",
        "test_runs.py",
    }
    assert forbidden.isdisjoint(path.name for path in tests.iterdir())


def test_shipped_recipes_do_not_describe_removed_subcall_runtime_state():
    recipes = Path(__file__).parents[3] / "recipes"
    forbidden = ("_subcall", "subcall spawns", "child scheduler", "RunIndex")
    for path in recipes.rglob("*.yaml"):
        content = path.read_text()
        assert not any(token in content for token in forbidden), path
