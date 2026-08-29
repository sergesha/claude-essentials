"""Recovery after crashes during destination publication."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lockstep.authoring_publisher import AuthoringPublisher
from tests._authoring_gate import tree_image
from tests._authoring_publisher_faults import _SimulatedProcessDeath
from tests._authoring_publisher_namespace import _is_destination_namespace_call
from tests._authoring_publisher_scenario import (
    _prepare_existing_bundle_scenario,
    _assert_existing_bundle_restored,
    _assert_durable_crash_cut,
)

@pytest.mark.parametrize("ordinal", (0, 1, 2))
def test_recover_restores_existing_bundle_after_each_destination_rename_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    assert len(scenario.destinations) == 3
    original_replace = os.replace
    replacement_count = 0
    crash_was_injected = False

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        nonlocal crash_was_injected, replacement_count
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if crash_was_injected or not _is_destination_namespace_call(
            scenario.destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        current_ordinal = replacement_count
        replacement_count += 1
        if current_ordinal == ordinal:
            crash_was_injected = True
            raise _SimulatedProcessDeath(
                "simulated process death after destination rename"
            )
        return result

    monkeypatch.setattr(os, "replace", replace_then_crash)
    owner_state_before_crash = tree_image(scenario.owner_state)
    with pytest.raises(_SimulatedProcessDeath):
        scenario.publisher.publish(scenario.bundle)

    assert crash_was_injected
    assert replacement_count == ordinal + 1
    _assert_durable_crash_cut(scenario, ordinal, owner_state_before_crash)
    monkeypatch.setattr(os, "replace", original_replace)

    fresh_publisher = AuthoringPublisher(scenario.owner_state)
    fresh_publisher.recover(scenario.project)

    _assert_existing_bundle_restored(scenario)
    project_before_second_recovery = tree_image(scenario.project)
    owner_before_second_recovery = tree_image(scenario.owner_state)
    fresh_publisher.recover(scenario.project)
    assert tree_image(scenario.project) == project_before_second_recovery
    assert tree_image(scenario.owner_state) == owner_before_second_recovery
