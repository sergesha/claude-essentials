"""Run PoC9 orchestration in the pinned runtime's Python environment."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
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
               'service_root':str(context.service.service_root),
               'runtime_root':str(identity.runtime_root), 'operation_id':context.operation_id,
               'vector_cache':str(context.service.service_root / 'vectors')}
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


def _native_store(service, request, payload):
    from .operations import atlas_path, operation_project, read_registry, retained_request
    from .project_registry import (graph_endpoint, project_database, project_store, repository_identity,
                                   resolve_project, verify_graph_owner)

    path = Path(payload['atlas_path'])
    binding = None
    if payload.get('operation_id') is not None:
        with read_registry(service) as db:
            if db is None:
                raise AtlasError('invalid_configuration', 'native execution requires its service registry')
            row = db.execute('SELECT * FROM operations WHERE operation_id=?', (payload['operation_id'],)).fetchone()
            if row is None or row['state'] != 'running' or row['evidence'] != payload['evidence'] or row['atlas_path'] != str(path):
                raise AtlasError('atlas_conflict', 'native dispatch differs from its running operation', 409)
            original = retained_request(row)
            if original.action == 'archive.restore':
                # Rebuild precedes registration. The running restore owns its destination.
                if (request.action != 'publication.rebuild' or atlas_path(service, original) != path
                        or repository_identity(path) != request.atlas_id):
                    raise AtlasError('atlas_conflict', 'native rebuild differs from the restored project', 409)
                graph = projection_graph(payload['configuration'])
                name = project_database(uuid.UUID(request.atlas_id))
                if name != graph['database']:
                    raise AtlasError('graph_atlas_conflict', 'native rebuild differs from the restored database', 409)
                endpoint = graph_endpoint(graph['url'])
                owner = db.execute('SELECT atlas_id FROM projection_owners WHERE endpoint=? AND database_name=?',
                                   (endpoint, name)).fetchone()
                if owner is None or owner['atlas_id'] != request.atlas_id:
                    raise AtlasError('graph_atlas_conflict', 'native rebuild lacks its prepared projection owner', 409)
                return project_store(service, path)
            frozen = json.loads(row['configuration'])
            if request.atlas_id != original.atlas_id or payload['configuration'] != frozen['configuration']:
                raise AtlasError('atlas_conflict', 'native dispatch differs from its frozen project configuration', 409)
            binding = operation_project(service, original, path, frozen)
    if binding is None:
        binding = resolve_project(service, request.atlas_id)
    if binding.path != path:
        raise AtlasError('atlas_conflict', 'native dispatch differs from the registered project path', 409)
    if request.action in ('publication.build', 'publication.rebuild'):
        graph = projection_graph(payload['configuration'])
        if graph['database'] != binding.graph_database:
            raise AtlasError('graph_atlas_conflict', 'native projection differs from the registered database', 409)
        with read_registry(service) as db:
            verify_graph_owner(db, graph_endpoint(graph['url']), binding.graph_database, binding.atlas_id, require_owner=True)
    return project_store(service, path)


def execute_native(payload):
    from .actions.sources import prepare_sources
    from .core.git_store import git
    from .core.ingest import ingest
    from .core.model_config import ConfiguredModels
    from .core.publication import Publications
    from .core.relations import propose_resolution, TypeDecisionUnavailable
    request = ActionRequest(**payload['request'])
    evidence = Path(payload['evidence'])
    from .contracts import ServiceContext
    root = payload.get('service_root')
    if not isinstance(root, str) or not Path(root).is_absolute() or str(Path(root).resolve()) != root:
        raise AtlasError('invalid_configuration', 'native execution requires the absolute service root')
    service = ServiceContext(Path(root), configuration=payload['configuration'])
    cache = service.service_root / 'vectors'
    if payload.get('vector_cache') != str(cache) or cache.resolve() != cache:
        raise AtlasError('invalid_configuration', 'native execution requires the service vector-cache location')
    store = _native_store(service, request, payload)
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
            from .contracts import ActionContext
            context = ActionContext(service, request, store.base, evidence, payload['configuration'])
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
    from .operations import database
    from .project_registry import (graph_endpoint, project_database, project_id, repository_identity,
                                   verify_graph_owner)

    graph = projection_graph(context.configuration)
    endpoint = graph_endpoint(graph['url'])
    name = graph['database']
    store = GitStore(context.atlas_path)
    identity = repository_identity(store.base)
    binding = context.project_binding
    if binding is None:
        raise AtlasError('graph_atlas_conflict', 'projection requires a current project binding', 409)
    if ((context.request.atlas_id is not None and project_id(context.request.atlas_id) != identity)
            or binding.atlas_id != identity or binding.path != store.base):
        raise AtlasError('atlas_conflict', 'the projection repository differs from the selected atlas', 409)
    if binding.graph_database != name or name != project_database(uuid.UUID(identity)):
        raise AtlasError('graph_atlas_conflict', 'the projection database differs from the selected atlas binding', 409)

    with database(context.service) as db:
        db.execute('BEGIN IMMEDIATE')
        registered = db.execute('SELECT path,graph_database FROM atlases WHERE atlas_id=?', (identity,)).fetchone()
        if registered:
            if Path(registered['path']) != store.base and Path(registered['path']).exists():
                raise AtlasError('atlas_conflict', 'this UUID is already registered at another existing location', 409)
            if registered['graph_database'] != name:
                raise AtlasError('graph_atlas_conflict', 'the projection database differs from the registered binding', 409)
        verify_graph_owner(db, endpoint, name, identity)
        owner = db.execute('SELECT 1 FROM projection_owners WHERE endpoint=? AND database_name=?',
                           (endpoint, name)).fetchone()
        if owner:
            return
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
    cache = Path(payload['vector_cache'])
    builder = NativeProjection(store, native, tokenizer, formatter, cache)
    try:
        if rebuilding:
            import sqlite3
            from .core.archive import rebuild_projections, seed_retained_vectors
            from .core.embedding_provider import initialize_cache
            from .core.publication import Publications
            initialize_cache(cache)
            with sqlite3.connect(cache / 'vectors.sqlite3') as db:
                seed_retained_vectors(Publications(store), db)
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
