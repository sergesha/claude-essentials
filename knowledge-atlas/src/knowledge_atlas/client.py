"""Typed Python HTTP client; the service retains mutation and review ownership."""
from dataclasses import asdict
import json
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .contracts import ActionRequest,ActionResult,OperationRecord,AtlasError
from . import __version__
from . import api_models as models


class AtlasClient:
    def __init__(self,service_root):
        self.root=Path(service_root).expanduser().resolve()
        descriptor=json.loads((self.root/'discovery.json').read_text())
        if descriptor['version']!=__version__:raise AtlasError('version_conflict','stop the existing service before changing package version',409)
        self.endpoint=descriptor['endpoint']
        url=urlsplit(self.endpoint)
        if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path or url.query or url.fragment or url.username:
            raise AtlasError('invalid_configuration','discovery must identify local HTTP service',422)
        self.token=(self.root/'token').read_text().strip()

    def request(self,path,body=None,*,timeout=30):
        request=urllib.request.Request(self.endpoint+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            value=json.load(error)['error']
            raise AtlasError(value['code'],value['message'],value['status']) from error
        except (urllib.error.URLError,OSError) as error:
            raise AtlasError('service_unavailable','local service is unavailable',503) from error

    def call(self,request:ActionRequest)->ActionResult|OperationRecord:
        value=self.request('/v1/actions/'+request.action,asdict(request),timeout=900 if request.action=='question.ask' else 30)
        return self.decode(value)

    @staticmethod
    def decode(value):
        if 'operation_id' in value:
            value['evidence']=Path(value['evidence'])
            if value.get('result') is not None:value['result']=ActionResult(**value['result'])
            return OperationRecord(**value)
        return ActionResult(**value)

    def operation(self,operation_id:str)->OperationRecord:
        return self.decode(self.request('/v1/operations/'+operation_id))

    def schema(self):return self.request('/v1/schema')
    def stop(self):return self.request('/v1/stop',{})


    def _typed(self,action,payload,*,atlas_id=None,request_id=None,expected_publication=None,
               publication=None,as_known_at=None,valid_at=None):
        return self.call(ActionRequest(action,atlas_id,request_id,expected_publication,
            payload.model_dump(exclude_unset=True),publication,as_known_at,valid_at))

    def atlas_create(self,payload:models.Location,**selection)->OperationRecord:
        return self._typed('atlas.create',payload,**selection)

    def atlas_open(self,payload:models.Location,**selection)->ActionResult:
        return self._typed('atlas.open',payload,**selection)

    def atlas_status(self,payload:models.Payload,**selection)->ActionResult:
        return self._typed('atlas.status',payload,**selection)

    def packages_inspect(self,payload:models.Payload,**selection)->ActionResult:
        return self._typed('packages.inspect',payload,**selection)

    def packages_pin(self,payload:models.Pin,**selection)->OperationRecord:
        return self._typed('packages.pin',payload,**selection)

    def sources_submit(self,payload:models.Sources,**selection)->OperationRecord:
        return self._typed('sources.submit',payload,**selection)

    def candidate_inspect(self,payload:models.Candidate,**selection)->ActionResult:
        return self._typed('candidate.inspect',payload,**selection)

    def candidate_decide(self,payload:models.Decision,**selection)->OperationRecord:
        return self._typed('candidate.decide',payload,**selection)

    def candidate_revise(self,payload:models.Revision,**selection)->OperationRecord:
        return self._typed('candidate.revise',payload,**selection)

    def type_decide(self,payload:models.TypeDecision,**selection)->OperationRecord:
        return self._typed('type.decide',payload,**selection)

    def search(self,payload:models.Search,**selection)->ActionResult:
        return self._typed('search',payload,**selection)

    def evidence_read(self,payload:models.Evidence,**selection)->ActionResult:
        return self._typed('evidence.read',payload,**selection)

    def knowledge_apply(self,payload:models.Application,**selection)->ActionResult:
        return self._typed('knowledge.apply',payload,**selection)

    def feedback_record(self,payload:models.Feedback,**selection)->OperationRecord:
        return self._typed('feedback.record',payload,**selection)

    def knowledge_revise(self,payload:models.KnowledgeRevision,**selection)->OperationRecord:
        return self._typed('knowledge.revise',payload,**selection)

    def history(self,payload:models.History,**selection)->ActionResult:
        return self._typed('history',payload,**selection)

    def diff(self,payload:models.Difference,**selection)->ActionResult:
        return self._typed('diff',payload,**selection)

    def archive_export(self,payload:models.Location,**selection)->OperationRecord:
        return self._typed('archive.export',payload,**selection)

    def archive_restore(self,payload:models.Restore,**selection)->OperationRecord:
        return self._typed('archive.restore',payload,**selection)

    def operation_read(self,payload:models.Operation,**selection)->OperationRecord:
        return self._typed('operation.read',payload,**selection)

    def operation_resume(self,payload:models.Operation,**selection)->OperationRecord:
        return self._typed('operation.resume',payload,**selection)

    def wiki_pages(self,payload:models.Payload,**selection)->ActionResult:
        return self._typed('wiki.pages',payload,**selection)

    def wiki_read(self,payload:models.WikiPage,**selection)->ActionResult:
        return self._typed('wiki.read',payload,**selection)

    def question_ask(self,payload:models.Question,**selection)->ActionResult:
        return self._typed('question.ask',payload,**selection)
