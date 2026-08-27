"""B1 staged surface for the private legacy-to-v2 schema transition."""

from __future__ import annotations

from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints

import pytest


def test_transition_surface_is_exact_and_fail_closed_without_io(
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
    with pytest.raises(
        NotImplementedError,
        match="runtime schema transition is not implemented",
    ):
        transition(database_path)

    assert not database_path.exists()
