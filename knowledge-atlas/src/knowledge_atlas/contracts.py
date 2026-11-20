"""Shared configuration types; constructing one never probes a runtime."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
OperationId = str
PublicationId = str

@dataclass(frozen=True)
class RuntimeConfig:
    runtime_root: Path
    manifest: Path | None = None

@dataclass(frozen=True)
class RuntimeIdentity:
    runtime_root: Path
    platform: str
    components: dict[str, str]
    profile_id: str

@dataclass(frozen=True)
class StageProfile:
    provider: str
    model: str
    effort: str | None = None
    guidance: str = ""
    identity: JsonObject = field(default_factory=dict)


class AtlasError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(message if message.startswith(code + ': ') else f'{code}: {message}')


@dataclass(frozen=True)
class ActionRequest:
    action: str
    atlas_id: str | None = None
    request_id: str | None = None
    expected_publication: str | None = None
    payload: JsonObject = field(default_factory=dict)
    publication: str | None = None
    as_known_at: str | None = None
    valid_at: str | None = None

    def __post_init__(self):
        import re
        import uuid
        from datetime import datetime
        if not isinstance(self.action, str) or not self.action or not isinstance(self.payload, dict):
            raise AtlasError('invalid_request', 'action and object payload required')
        if self.request_id is not None:
            try:
                uuid.UUID(self.request_id)
            except (ValueError, TypeError, AttributeError) as error:
                raise AtlasError('invalid_request', 'request_id must be a UUID') from error
        for oid in (self.expected_publication, self.publication):
            if oid is not None and (not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid)):
                raise AtlasError('invalid_request', 'exact publication OID required')
        if self.publication is not None and self.as_known_at is not None:
            raise AtlasError('invalid_request', 'publication and as_known_at are mutually exclusive')
        for value in (self.as_known_at, self.valid_at):
            if value is not None:
                try:
                    if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
                        raise ValueError('timezone required')
                except (ValueError, TypeError, AttributeError) as error:
                    raise AtlasError('invalid_request', 'timezone-aware timestamp required') from error


@dataclass(frozen=True)
class ActionResult:
    data: JsonObject = field(default_factory=dict)
    publication: str | None = None
    evidence: list[str] = field(default_factory=list)
    canonical_effects: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    state: str
    attempt: int
    evidence: Path
    result: ActionResult | None = None
    error: JsonObject | None = None


@dataclass
class ServiceContext:
    service_root: Path
    runtime: RuntimeConfig | None = None
    configuration: JsonObject = field(default_factory=dict)
    handlers: dict = field(default_factory=dict, repr=False)
    recoveries: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.service_root = Path(self.service_root).expanduser().resolve()


@dataclass
class ActionContext:
    service: ServiceContext
    request: ActionRequest
    atlas_path: Path
    evidence: Path
    configuration: JsonObject
    operation_id: str | None = None
    previous_evidence: Path | None = None

    @classmethod
    def for_read(cls, service, request):
        from .operations import atlas_path
        return cls(service, request, atlas_path(service, request), service.service_root, service.configuration)
