from pathlib import Path


def test_forbidden_legacy_lifecycle_tests_are_removed():
    tests = Path(__file__).parents[1]
    forbidden = {
        "_subcall_helpers.py",
        "test_engine_subcalls.py",
        "test_integration_subcalls.py",
        "test_daily_change_recipe.py",
        "test_runs.py",
    }
    assert forbidden.isdisjoint(path.name for path in tests.iterdir())
