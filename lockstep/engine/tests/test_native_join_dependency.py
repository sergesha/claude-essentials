"""The pinned yamlgraph patch delegates explicit all-source joins to LangGraph."""

from pathlib import Path

import pytest
import yaml


def _document():
    return {
        "version": "1.0", "name": "native-join",
        "state": {"joined": "bool"},
        "nodes": {
            "left": {"type": "passthrough"},
            "right": {"type": "passthrough"},
            "right_end": {"type": "passthrough"},
            "join": {"type": "passthrough", "output": {"joined": True}},
        },
        "edges": [
            {"from": "START", "to": ["left", "right"]},
            {"from": "right", "to": "right_end"},
            {"from": ["left", "right_end"], "to": "join"},
            {"from": "join", "to": "END"},
        ],
    }


def test_native_join_waits_for_unequal_branches_and_fires_once(tmp_path: Path):
    from yamlgraph.compile.graph_loader import compile_graph, load_graph_config

    source = tmp_path / "join.yaml"
    source.write_text(yaml.safe_dump(_document()))
    app = compile_graph(load_graph_config(source)).compile()
    updates = list(app.stream({}))
    assert sum("join" in update for update in updates) == 1
    assert next(i for i, item in enumerate(updates) if "right_end" in item) < next(
        i for i, item in enumerate(updates) if "join" in item
    )


@pytest.mark.parametrize("change", [
    {"from": []}, {"from": ["left", "left"]},
    {"from": ["left", "missing"]}, {"to": ["join"]},
    {"condition": "joined == true"},
])
def test_native_join_rejects_ambiguous_or_invalid_sources(change):
    from yamlgraph.models.graph_schema import validate_graph_schema

    document = _document()
    document["edges"][2].update(change)
    with pytest.raises(ValueError):
        validate_graph_schema(document)


def test_native_join_renders_each_source_with_all_label():
    from yamlgraph.mermaid_export import render_mermaid

    rendered = render_mermaid(_document())
    assert 'left -->|"all"| join' in rendered
    assert 'right_end -->|"all"| join' in rendered
