"""Transport-neutral selected reads over the retained PoC9 reader."""
import json
from urllib.parse import urlsplit
from ..contracts import ActionRequest, ActionResult, AtlasError
from ..core.reader import AtlasReader
from ..operations import database
from ..project_registry import graph_endpoint, project_store, verify_graph_owner


def selected_reader(context):
    store = project_store(context.service, context.atlas_path)
    graph = context.configuration.get('graph', {})
    url = graph.get('url', '')
    if url:
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
            raise AtlasError('unsupported_configuration', 'graph runtime must use local HTTP', 422)
    def verify_database(database_name):
        if context.project_binding is None:
            return
        endpoint = graph_endpoint(url) if url else None
        with database(context.service) as db:
            verify_graph_owner(db, endpoint, database_name, context.project_binding.atlas_id,
                               require_owner=bool(url))

    try:
        allowed = set()
        reader = AtlasReader(store, url, lambda: allowed, context.request.publication,
                             as_known_at=context.request.as_known_at, verify_database=verify_database)
        # The local owner can read retained history. Eligibility below narrows
        # retrieval, without erasing older provenance from the access policy.
        for publication in reader.chain:
            for record in reader._records(publication).values():
                allowed.add(record['@id'])
                allowed.update(record.get('links', []))
        return reader
    except (ValueError, KeyError, OSError) as error:
        raise AtlasError('publication_unavailable', str(error), 503) from error


def read_action(context):
    request, data = context.request, context.request.payload
    reader = selected_reader(context)
    if request.action in ('wiki.pages','wiki.read','question.ask'):
        from .viewer_reads import explore
        return explore(context,reader)
    try:
        if request.action == 'search':
            count = data.get('count', 10)
            if type(count) is not int or not 1 <= count <= 1000:
                raise AtlasError('invalid_request', 'count must be between 1 and 1000')
            eligible = data.get('eligible')
            if eligible is not None and (not isinstance(eligible,list) or not all(isinstance(r,str) for r in eligible)):
                raise AtlasError('invalid_request', 'eligible must be an array of record IDs')
            result = reader.search(data.get('query'), mode=data.get('mode','hybrid'), count=count,
                eligible=eligible, purpose={'application':'recommendation'}.get(data.get('purpose'),data.get('purpose','research')))
        elif request.action == 'evidence.read':
            result = reader.retrieve_evidence(_record_id(data))
            if result is not None and data.get('expand'):
                depth, limit = data.get('depth',1), data.get('limit',100)
                if type(depth) is not int or not 0 <= depth <= 10 or type(limit) is not int or not 1 <= limit <= 1000:
                    raise AtlasError('invalid_request','bounded depth (0..10) and limit (1..1000) required')
                expansion = reader.graph_neighborhood(data['record_id'],depth=depth,result_limit=limit)
                _projection_result(expansion)
                result['expansion'] = expansion
        elif request.action == 'knowledge.apply':
            application_context = data.get('context')
            if not isinstance(application_context,dict): raise AtlasError('invalid_request','application context required')
            result = reader.answer(_record_id(data), application_context, valid_at=request.valid_at)
        elif request.action == 'history':
            if data.get('record_id'):
                result = {'history':reader.history(_record_id(data))}
            else:
                current = reader.publication; history = []
                while current:
                    # Timeline transport carries metadata, not native vector arrays.
                    # Full projection artifacts remain in the Git publication.
                    metadata = {key:value for key,value in reader.chain[current].items()
                                if key not in ('vector_artifacts','native_export_excluded_ids')}
                    history.append({'publication_commit':current, **metadata,
                                    'matching_decision_receipt':reader.matching_decision_receipt(current)})
                    current = reader.chain[current]['previous_publication']
                result = {'history':history}
        elif request.action == 'diff':
            for field in ('before','after'):
                if not data.get(field): raise AtlasError('invalid_request',field+' publication is required')
                ActionRequest('diff',publication=data[field])
            result = {'changes':reader.diff(data['before'],data['after'],data.get('record_id'))}
        else:
            raise AtlasError('unsupported_action',request.action,422)
    except (ValueError, TypeError) as error:
        raise AtlasError('invalid_request',str(error)) from error
    if result is None: raise AtlasError('record_not_found','record unavailable in the selected publication',404)
    _projection_result(result)
    evidence = []
    if request.action == 'knowledge.apply' and data.get('explain'):
        from ..core.model_config import ConfiguredModels
        from ..runtime import _assets
        import uuid
        location = context.service.service_root/'read-attempts'/str(uuid.uuid4())
        models = ConfiguredModels(config=context.configuration.get('profiles'),evidence=location)
        ids = {data['record_id'], *[ground['id'] for ground in result.get('grounds', [])]}
        supplied = [reader.retrieve_evidence(rid) for rid in sorted(ids)]
        supplied = [item for item in supplied if item is not None]
        explanation = models({'operation':'grounded_answer',
            'instruction':(_assets()/'grounded-instructions.txt').read_text(),
            'question':data.get('question','Explain applicability in the supplied context.'),
            'context':data['context'],'assessment':result,'evidence':supplied})
        if (not isinstance(explanation,dict) or not isinstance(explanation.get('answer'),str)
                or not isinstance(explanation.get('citations'),list)
                or not all(isinstance(rid,str) and rid in ids for rid in explanation['citations'])
                or not isinstance(explanation.get('unknowns'),list)
                or not all(isinstance(item,str) for item in explanation['unknowns'])):
            raise AtlasError('invalid_model_response','explanation must cite supplied evidence and retain unknowns',503)
        result = {**result,'explanation':{key:explanation[key] for key in ('answer','citations','unknowns')}}
        evidence.append(str(location))
    return ActionResult(data=result,publication=reader.publication,evidence=evidence)


def _record_id(data):
    value = data.get('record_id')
    if not isinstance(value,str) or not value: raise AtlasError('invalid_request','record_id is required')
    return value


def _projection_result(result):
    if result and result.get('status') == 'technical_error':
        raise AtlasError('projection_unavailable',result.get('reason','exact projection unavailable'),503)
