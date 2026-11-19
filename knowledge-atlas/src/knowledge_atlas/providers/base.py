"""The two existing PoC9 model seams, shared by supported hosts."""
from pathlib import Path
from typing import Protocol
from ..contracts import JsonObject, StageProfile


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Provider(Protocol):
    def identity(self, profile: StageProfile, stage: str) -> JsonObject: ...
    def structured(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject: ...
    async def wiki(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject: ...
