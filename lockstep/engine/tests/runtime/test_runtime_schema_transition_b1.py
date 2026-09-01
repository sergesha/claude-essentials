"""B1 surface for the private legacy-to-v2 schema transition."""

from __future__ import annotations

from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints

def test_transition_surface_is_exact_and_absent_store_is_noop(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.storage import RuntimeSchemaMigrator

    descriptor = vars(RuntimeSchemaMigrator).get("transition_legacy_to_v2")
    assert isinstance(descriptor, classmethod)
    transition = RuntimeSchemaMigrator.transition_legacy_to_v2
    assert tuple(
        (parameter.name, parameter.kind)
        for parameter in signature(transition).parameters.values()
    ) == (("path", Parameter.POSITIONAL_OR_KEYWORD),)
    assert get_type_hints(transition) == {
        "path": Path,
        "return": type(None),
    }

    database_path = tmp_path / "absent-runtime.sqlite"
    transition(database_path)

    assert not database_path.exists()
    assert tuple(tmp_path.iterdir()) == ()
