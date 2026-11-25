"""Typed Python HTTP client; the service retains mutation and review ownership."""
from dataclasses import asdict
import hmac
import http.client
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from .contracts import ActionRequest,ActionResult,OperationRecord,AtlasError
from . import __version__
from . import api_models as models


class AtlasClient:
    def __init__(self,service_root, *, channel="python", pin_identity=False):
        self.channel = channel
        self.root=Path(service_root).expanduser().resolve()
        discovery = self._discovery()
        self._pinned_discovery = discovery if pin_identity else None

    def verify_ready(self) -> str:
        status = self.request('/v1/status', timeout=3)
        if not isinstance(status, dict) or status.get('endpoint') != self.endpoint:
            raise AtlasError('service_unavailable', 'local service did not report its verified endpoint', 503)
        if status.get('version') != __version__:
            raise AtlasError('version_conflict', 'stop the existing service before changing package version', 409)
        return status['version']

    def _discovery(self):
        try:
            descriptor = json.loads((self.root / 'discovery.json').read_text())
            token = (self.root / 'token').read_text().strip()
        except FileNotFoundError as error:
            raise AtlasError('service_unavailable', 'local service is unavailable', 503) from error
        except (OSError, ValueError) as error:
            raise AtlasError('invalid_configuration', 'unreadable local service discovery', 422) from error
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get('endpoint'), str):
            raise AtlasError('invalid_configuration', 'invalid local service discovery', 422)
        if descriptor.get('version') != __version__:
            raise AtlasError('version_conflict', 'stop the existing service before changing package version', 409)
        endpoint = descriptor['endpoint']
        try:
            url = urlsplit(endpoint)
            port = url.port
        except ValueError as error:
            raise AtlasError('invalid_configuration', 'invalid local service discovery', 422) from error
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not port
                or url.path or url.query or url.fragment or url.username or not token):
            raise AtlasError('invalid_configuration', 'invalid local service discovery', 422)
        self.endpoint = endpoint
        return url, token

    def _verified_connection(self, timeout):
        # MCP sessions retain the discovery identity they verified at binding.
        url, token = self._pinned_discovery or self._discovery()
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
        try:
            # Verify this socket's peer before sending credentials or atlas data.
            # Disable automatic reconnect, including after a successful proof.
            connection.connect()
            connection.auto_open = False
            nonce = secrets.token_hex(32)
            connection.request('GET', '/v1/identity', headers={'Atlas-Challenge': nonce})
            response = connection.getresponse()
            expected = hmac.digest(token.encode(), (url.geturl() + '/' + nonce).encode(), 'sha256').hex()
            if (response.status != 200 or response.read(256) != expected.encode()
                    or connection.sock is None):
                raise AtlasError('service_unavailable', 'local service identity could not be verified', 503)
            return connection, token
        except BaseException:
            connection.close()
            raise

    def request(self, path, body=None, *, timeout=30):
        # Only the credential-free handshake can be retried. Never replay an action.
        for attempt in range(3):
            try:
                connection, token = self._verified_connection(timeout)
                break
            except (OSError, http.client.HTTPException) as error:
                if attempt == 2:
                    raise AtlasError('service_unavailable', 'local service is unavailable', 503) from error
                time.sleep(0.05 * (2 ** attempt))
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request('GET' if data is None else 'POST', path, body=data,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                         'Connection': 'close', 'Atlas-Channel': self.channel})
            response = connection.getresponse()
            value = json.load(response)
            if response.status >= 400:
                detail = value['error']
                raise AtlasError(detail['code'], detail['message'], detail['status'])
            return value
        except (OSError, http.client.HTTPException) as error:
            raise AtlasError('service_unavailable', 'local service is unavailable', 503) from error
        finally:
            connection.close()

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

    def atlas_list(self) -> ActionResult:
        return self.call(ActionRequest('atlas.list'))

    def atlas_open(self, *, atlas_id: str) -> ActionResult:
        return self.call(ActionRequest('atlas.open', atlas_id=atlas_id))

    def atlas_status(self,payload:models.Payload,**selection)->ActionResult:
        return self._typed('atlas.status',payload,**selection)

    def packages_inspect(self,payload:models.Payload,**selection)->ActionResult:
        return self._typed('packages.inspect',payload,**selection)

    def packages_configure(self, payload: models.ConfigurePackages, *, atlas_id: str,
                           request_id: str, expected_publication: str | None) -> OperationRecord:
        return self._typed('packages.configure', payload, atlas_id=atlas_id, request_id=request_id,
                           expected_publication=expected_publication)

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
