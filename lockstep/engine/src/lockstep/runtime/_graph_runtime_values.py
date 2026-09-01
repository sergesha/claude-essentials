"""Values shared by GraphRuntime responsibility bases."""

from __future__ import annotations

from dataclasses import dataclass

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot


class NativeCoordinateRejected(ValueError):
    """A resume source is stale, foreign, or no longer pending."""


class RuntimeBindingConflict(RuntimeError):
    """A public run is already bound to a different immutable identity."""


MAX_HISTORY_SNAPSHOTS = 1024
MAX_HISTORY_INTERRUPTS = 4096


@dataclass(frozen=True)
class NativeCommitment:
    """Exact graph-owned facts observed under native invocation serialization."""

    binding: RunBinding
    snapshot: NativeSnapshot
    interrupt: NativeInterrupt
