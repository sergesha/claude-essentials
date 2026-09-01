"""Compatibility facade for validator registry and execution."""

from lockstep.runtime.validator_baselines import (
    _path_covered as _path_covered,
    build_manifest,
)
from lockstep.runtime.validator_execution import CHECKS, run_checks

__all__ = ["CHECKS", "build_manifest", "run_checks"]
