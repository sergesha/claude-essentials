"""Run PoC9 orchestration in the pinned runtime's Python environment."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from .contracts import ActionRequest, ActionResult, AtlasError
from .runtime import validate_runtime, RuntimeMismatch


def run_native(context):
    runtime = context.service.runtime
    if runtime is None:
        raise AtlasError('runtime_unavailable', 'configure and prepare the native runtime first', 503)
    try:
        identity = validate_runtime(runtime)
    except RuntimeMismatch as error:
        raise AtlasError(error.code, str(error), 503) from error
    if context.request.action in ('publication.build', 'publication.rebuild'):
        claim_projection_database(context)
    context.evidence.mkdir(parents=True, exist_ok=True)
    payload = {'request':asdict(context.request), 'atlas_path':str(context.atlas_path),
               'evidence':str(context.evidence), 'configuration':context.configuration,
               'runtime_root':str(identity.runtime_root), 'operation_id':context.operation_id}
    environment = {**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parent.parent),
                   'KNOWLEDGE_ATLAS_WIKI_ROOT':str(identity.runtime_root/'wiki'),
                   'POC9_EXTRACT_BIN':str(identity.runtime_root/'extract/target/debug/poc9-extract')}
    (context.evidence/'native-request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    with (context.evidence/'native-result.json').open('w') as output, (context.evidence/'native-stderr.txt').open('w') as errors:
        completed = subprocess.run([str(identity.runtime_root/'python/bin/python'), '-m', 'knowledge_atlas.native_pipeline'],
            input=json.dumps(payload, ensure_ascii=False), text=True, stdout=output, stderr=errors,
            env=environment, cwd=context.evidence)
    try:
        result = json.loads((context.evidence/'native-result.json').read_text())
    except ValueError as error:
        raise AtlasError('native_failed', 'native process returned no result; inspect attempt evidence', 503) from error
    if completed.returncode:
        raise AtlasError(result.get('code','native_failed'), result.get('message','native process failed'), result.get('status',503))
    return ActionResult(**result)


def execute_native(payload):
    from .actions.sources import prepare_sources
    from .core.git_store import GitStore, git
    from .core.ingest import ingest
    from .core.model_config import ConfiguredModels
    from .core.publication import Publications
    from .core.relations import propose_resolution, TypeDecisionUnavailable
    request = ActionRequest(**payload['request'])
    evidence = Path(payload['evidence'])
    store = GitStore(payload['atlas_path'])
    pubs = Publications(store)
    data = request.payload
    if request.action in ('publication.build', 'publication.rebuild'):
        return project_native(payload, store)
    revision = request.action == 'candidate.revise'
    recorded_at = data.get('decided_at') if revision else data.get('recorded_at')
    from datetime import datetime
    try:
        if datetime.fromisoformat(recorded_at.replace('Z','+00:00')).tzinfo is None: raise ValueError()
    except (ValueError, AttributeError, TypeError) as error:
        raise AtlasError('invalid_request', 'recorded_at requires a timezone-aware timestamp') from error
    if request.action == 'sources.submit' or revision:
        selected = pubs.read(request.expected_publication)['content_commit'] if request.expected_publication else git(store.root,'rev-parse','HEAD')
        if revision:
            from .actions.decisions import _binding, _check_base
            from .contracts import ActionContext, ServiceContext
            context = ActionContext(ServiceContext(evidence/'service'), request, store.base, evidence, payload['configuration'])
            _check_base(store, _binding(context, 'revise'))
            selected = data['candidate']
            if not data.get('sources') and not data.get('remove_source_ids') and not data.get('instructions'):
                raise AtlasError('invalid_request', 'revision requires instructions, sources or removals')
            if 'instructions' in data and (not isinstance(data['instructions'], str) or not data['instructions'].strip()):
                raise AtlasError('invalid_request', 'instructions must be nonempty text')
        previous = store.read(selected)
        prepared = prepare_sources(data.get('sources', []), data.get('remove_source_ids', []), previous.get('wiki', {}), evidence/'sources')
    elif request.action == 'type.decide':
        if any(not isinstance(data.get(key), str) or not data[key].strip()
               for key in ('candidate','decision_id','action','actor','reason')):
            raise AtlasError('invalid_request', 'explicit candidate, decision_id, action, actor and reason required')
        ActionRequest('candidate.inspect', publication=data['candidate'])
        if data['action'] not in ('accept','reuse','reject'):
            raise AtlasError('invalid_request', 'type decision must accept, reuse or reject')
        if data['action'] == 'reuse' and (not isinstance(data.get('type_id'), str) or not data['type_id'].strip()):
            raise AtlasError('invalid_request', 'reuse requires an explicit existing type_id')
    else:
        raise AtlasError('unsupported_action', request.action, 422)
    models = ConfiguredModels(config=payload['configuration'].get('profiles'), evidence=evidence/'model-calls')
    if request.action == 'sources.submit' or revision:
        async def wiki_agent(wiki_request):
            if revision and data.get('instructions'):
                wiki_request = {**wiki_request, 'correction_instructions': data['instructions']}
            return await models.wiki(wiki_request)
        execution = asyncio.run(ingest(store, evidence/'native', prepared['sources'], recorded_at=recorded_at,
            model=models, agent=wiki_agent, previous_commit=selected if request.expected_publication or revision or 'wiki' in previous else None,
            remove_sources=prepared['remove_sources'], model_profile=models.effective_profile('open'),
            relation_model_profile=models.effective_profile('phrase')))
        candidate = execution['candidate']['commit']
        # Initial ingests also need a durable candidate reference.
        git(store.root,'update-ref','refs/poc9/proposals/'+candidate,candidate)
    else:
        try:
            candidate = propose_resolution(store, data['candidate'], actor=data['actor'], recorded_at=recorded_at,
                base_publication=request.expected_publication, model=models,
                human={key:data[key] for key in ('decision_id','action','reason','type_id') if key in data})
        except TypeDecisionUnavailable as error:
            raise AtlasError('type_decision_unavailable', str(error), 422) from error
    snapshot = store.read(candidate)
    return ActionResult(data={'candidate':candidate, 'status':'pending_review',
        'unresolved':[row for row in snapshot.get('relations',{}).get('relations',[]) if row['status']!='resolved']},
        publication=pubs.ready(), evidence=[str(evidence)], canonical_effects={'candidate':candidate})


def projection_graph(configuration):
    graph = configuration.get('graph', {})
    if not isinstance(graph, dict) or not graph.get('url') or not graph.get('database'):
        raise AtlasError('runtime_unavailable', 'configure the prepared local graph runtime', 503)
    url = urlsplit(graph['url'])
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.query or url.fragment:
        raise AtlasError('unsupported_configuration', 'graph runtime must use local HTTP', 422)
    return graph


def claim_projection_database(context):
    from .core.git_store import GitStore
    from .core.publication import Publications
    from .core.reader import projection_receipt
    from .operations import database
    from .project_registry import graph_endpoint, project_id, repository_identity, verify_graph_owner

    graph = projection_graph(context.configuration)
    endpoint = graph_endpoint(graph['url'])
    name = graph['database']
    store = GitStore(context.atlas_path)
    identity = repository_identity(store.base)
    binding = context.project_binding
    if ((context.request.atlas_id is not None and project_id(context.request.atlas_id) != identity)
            or (binding and (binding.atlas_id != identity or binding.path != store.base))):
        raise AtlasError('atlas_conflict', 'the projection repository differs from the selected atlas', 409)
    if binding and binding.graph_database != name:
        raise AtlasError('graph_atlas_conflict', 'the projection database differs from the selected atlas binding', 409)

    with database(context.service) as db:
        db.execute('BEGIN IMMEDIATE')
        registered = db.execute('SELECT path,graph_database FROM atlases WHERE atlas_id=?', (identity,)).fetchone()
        if registered:
            if Path(registered['path']) != store.base and Path(registered['path']).exists():
                raise AtlasError('atlas_conflict', 'this UUID is already registered at another existing location', 409)
            if registered['graph_database'] not in (None, name):
                raise AtlasError('graph_atlas_conflict', 'the projection database differs from the registered binding', 409)
        verify_graph_owner(db, endpoint, name, identity)
        owner = db.execute('SELECT 1 FROM projection_owners WHERE endpoint=? AND database_name=?',
                           (endpoint, name)).fetchone()
        if owner:
            return
        # A legacy project can have a READY receipt predating the ownership table.
        for row in db.execute('SELECT path FROM atlases WHERE atlas_id<>? AND graph_database IS NULL', (identity,)):
            other = GitStore(row['path'])
            if not (other.root / '.git').exists():
                continue
            pubs = Publications(other)
            ready = pubs.ready()
            if ready and projection_receipt(other, ready, pubs.read(ready)).get('database') == name:
                raise AtlasError('graph_atlas_conflict', 'the graph database belongs to another atlas', 409)
        db.execute('INSERT INTO projection_owners VALUES (?,?,?)', (endpoint, name, identity))


def project_native(payload, store):
    from .core.graph_stage import GraphStage
    from .core.native_projection import NativeProjection
    graph = projection_graph(payload['configuration'])
    content = payload['request']['payload'].get('content')
    rebuilding = payload['request']['action'] == 'publication.rebuild'
    if not rebuilding:
        if not content: raise AtlasError('invalid_request', 'accepted content is required')
        ActionRequest('publication.build', publication=content)
    runtime = Path(payload['runtime_root'])
    native = GraphStage(graph['url'], graph['database'])
    assets = store.base/'assets'
    tokenizer = assets/'tokenizer.json' if assets.exists() else runtime/'model/tokenizer.json'
    formatter = assets/'chunking.py' if assets.exists() else runtime/'chunking.py'
    builder = NativeProjection(store, native, tokenizer, formatter, store.base/'vectors')
    try:
        if rebuilding:
            from .core.archive import rebuild_projections
            return ActionResult(data=rebuild_projections(store, builder), evidence=[payload['evidence']])
        return ActionResult(data=builder.build(content), evidence=[payload['evidence']])
    finally:
        (Path(payload['evidence'])/'projection-trace.json').write_text(json.dumps(native.trace, ensure_ascii=False, indent=2))


def main():
    try:
        result = execute_native(json.load(sys.stdin))
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    except Exception as error:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'code':getattr(error,'code','native_failed'),'message':str(error),'status':getattr(error,'status',503)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
