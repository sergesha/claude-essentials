"""Fault injection shared by authoring publication and recovery tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._authoring_publisher_namespace import _is_destination_namespace_call


class _SimulatedProcessDeath(BaseException):
    """Bypass in-process rollback while Python still releases test resources."""




@dataclass(slots=True)
class _DestinationReplacementFault:
    injected: bool = False




def _install_first_destination_replacement_fault(
    monkeypatch: pytest.MonkeyPatch, destinations: tuple[Path, ...]
) -> _DestinationReplacementFault:
    original_replace = os.replace
    fault = _DestinationReplacementFault()

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if fault.injected or not _is_destination_namespace_call(
            destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        fault.injected = True
        raise _SimulatedProcessDeath(
            "simulated process death after destination rename"
        )

    monkeypatch.setattr(os, "replace", replace_then_crash)
    return fault
