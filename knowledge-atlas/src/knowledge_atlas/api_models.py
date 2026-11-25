"""One typed model catalogue for HTTP validation, MCP schemas and Python calls."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, TypeAdapter
from .contracts import ActionRequest, ActionResult, OperationRecord, AtlasError


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)


class Location(Payload):
    path: str


class Source(Payload):
    source_id: str
    name: str
    content: str
    source_time: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class Sources(Payload):
    sources: list[Source] = []
    remove_source_ids: list[str] = []
    recorded_at: str


class Pin(Payload):
    path: str
    package_id: str
    commit: str
    mount: str
    recorded_at: str
    candidate: str | None = None
    scope: Literal['all'] = 'all'


class IncludeSelection(Payload):
    package_id: str
    mount: str
    publication: str


class ConfigurePackages(Payload):
    includes: list[IncludeSelection]


class Candidate(Payload):
    candidate: str


class Decision(Candidate):
    action: Literal['accept','reject']
    actor: str
    kind: Literal['human','synthetic']
    reason: str
    decided_at: str


class Revision(Candidate):
    actor: str
    kind: Literal['human','synthetic']
    reason: str
    decided_at: str
    entries: list[dict[str,Any]] | None = None
    instructions: str | None = None
    sources: list[Source] = []
    remove_source_ids: list[str] = []


class TypeDecision(Candidate):
    decision_id: str
    action: Literal['accept','reuse','reject']
    type_id: str | None = None
    actor: str
    reason: str
    recorded_at: str


class Search(Payload):
    query: str
    mode: Literal['vector','fts','hybrid'] = 'hybrid'
    count: int = 10
    eligible: list[str] | None = None
    purpose: Literal['research','application'] = 'research'


class Evidence(Payload):
    record_id: str
    expand: bool = False
    depth: int = 1
    limit: int = 100


class Application(Payload):
    record_id: str
    context: dict[str,Any]
    explain: bool = False
    question: str | None = None


class WikiPage(Payload):
    page_id: str


class Question(Payload):
    question: str
    count: int = 5


class Feedback(Payload):
    record_id: str
    applied_publication: str
    raw_report: str
    actor: str
    recorded_at: str


class KnowledgeRevision(Payload):
    record_id: str
    actor: str
    reason: str
    recorded_at: str
    kind: Literal['human','synthetic'] | None = None
    entries: list[dict[str,Any]] | None = None
    instructions: str | None = None
    sources: list[Source] = []
    remove_source_ids: list[str] = []


class History(Payload):
    record_id: str | None = None


class Difference(History):
    before: str
    after: str


class Restore(Location):
    archive: str


class Operation(Payload):
    operation_id: str


PAYLOADS = {'wiki.pages':Payload,'wiki.read':WikiPage,'question.ask':Question,'atlas.create':Location,'atlas.list':Payload,'atlas.open':Payload,'atlas.status':Payload,
    'packages.inspect':Payload,'packages.pin':Pin,'packages.configure':ConfigurePackages,'sources.submit':Sources,'candidate.inspect':Candidate,
    'candidate.decide':Decision,'candidate.revise':Revision,'type.decide':TypeDecision,'search':Search,
    'evidence.read':Evidence,'knowledge.apply':Application,'feedback.record':Feedback,
    'knowledge.revise':KnowledgeRevision,'history':History,'diff':Difference,
    'archive.export':Location,'archive.restore':Restore,'operation.read':Operation,'operation.resume':Operation}
MODELS = {name:create_model(name.replace('.','_')+'Request', __config__=ConfigDict(extra='forbid',strict=True),
    action=(Literal[name],name), atlas_id=(str, ...) if name in ('atlas.open', 'packages.configure') else (str|None,None),
    request_id=(str, ...) if name == 'packages.configure' else (str|None,None),
    expected_publication=(str|None, ... if name == 'packages.configure' else None), publication=(str|None,None), as_known_at=(str|None,None),
    valid_at=(str|None,None), payload=(payload, ... if any(f.is_required() for f in payload.model_fields.values())
                                      else Field(default_factory=payload))) for name,payload in PAYLOADS.items()}


def parse_request(action, body, *, models=MODELS):
    if action not in models:raise AtlasError('unsupported_action',action,422)
    try:
        model=models[action].model_validate(body)
        value=model.model_dump(exclude_unset=True)
        value['action']=action
        return ActionRequest(**value)
    except (ValidationError,TypeError,ValueError) as error:
        raise AtlasError('invalid_request',str(error),400) from error


def schema():
    from . import __version__
    return {'version':__version__,
        'results':{model.__name__:TypeAdapter(model).json_schema() for model in (ActionResult,OperationRecord)},
        'actions':{name:{'input':model.model_json_schema()} for name,model in MODELS.items()}}
