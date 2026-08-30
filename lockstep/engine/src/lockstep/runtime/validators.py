"""Compatibility facade for validator registry and execution."""

from lockstep.runtime.validator_baselines import build_manifest, _path_covered
from lockstep.runtime.validator_execution import CHECKS, run_checks

__all__ = ["CHECKS", "build_manifest", "run_checks"]
