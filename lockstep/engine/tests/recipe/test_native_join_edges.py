"""Strict native graph ingress preserves AND joins without widening conditions."""

from pathlib import Path

import pytest
import yaml

from lockstep.recipe.authority import StrictRecipeIngress
from lockstep.recipe.profile import check_recipe


def _recipe(tmp_path: Path, join):
    path = tmp_path / "join.recipe.yaml"
    path.write_text(yaml.safe_dump({
        "version": "1.0", "name": "join",
        "nodes": {name: {"type": "passthrough"} for name in ("left", "right", "joined")},
        "edges": [{"from": "START", "to": ["left", "right"]}, join,
                  {"from": "joined", "to": "END"}],
    }))
    return path


def test_native_join_is_admitted_and_profile_checked(tmp_path: Path):
    path = _recipe(tmp_path, {"from": ["left", "right"], "to": "joined"})
    StrictRecipeIngress(tmp_path).inspect(path.name)
    assert check_recipe(path) == []


@pytest.mark.parametrize("join", [
    {"from": [], "to": "joined"},
    {"from": ["left", 1], "to": "joined"},
    {"from": ["left", "right"], "to": "joined", "condition": "ready == true"},
    {"from": ["left", "right"], "to": ["joined"]},
])
def test_native_join_rejects_unsupported_shapes(tmp_path: Path, join):
    path = _recipe(tmp_path, join)
    with pytest.raises(ValueError):
        StrictRecipeIngress(tmp_path).inspect(path.name)
