"""A child process executes one persisted attempt under service and atlas locks."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import traceback
from .contracts import ActionContext, ActionRequest, ActionResult, AtlasError, RuntimeConfig
from .locks import FileLock, AtlasLock
from .operations import initialize_service_storage, database, encoded, operation_project, staged_restore_input, validate_atlas_path
from .project_registry import project_configuration
from .actions import register_handlers, register_recoveries


def run_attempt(context, handler):
    if context.previous_evidence is not None:
        recover = register_recoveries(context.service).get(context.request.action)
        if recover is None:
            raise AtlasError('unsafe_resume', 'no canonical reconciliation is available; submit a new reviewed request', 409)
        result = recover(context)
        if result is not None:
            return result
    from .core.git_store import GitStore
    from .core.publication import Publications
    store = GitStore(context.atlas_path)
    current = Publications(store).ready() if (store.root/'.git').exists() else None
    if current != context.request.expected_publication:
        if context.request.action in ('candidate.decide', 'candidate.revise'):
            # A restored Git receipt can outlive both its operation database and its base READY.
            from .actions.decisions import replay_recorded_decision
            replay = replay_recorded_decision(context)
            if replay is not None:
                return replay
        raise AtlasError('stale_publication', 'READY changed; revalidate the request', 409)
    return handler(context)


def run_next(service):
    initialize_service_storage(service)
    try:
        with FileLock(service.service_root/'writer.lock'):
            _execute_next(service)
    except AtlasError as error:
        if error.code != 'busy': raise


def _execute_next(service):
    with database(service) as db:
        row = db.execute("SELECT * FROM operations WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
    if row is None: return
    evidence = Path(row['evidence'])
    with FileLock(evidence/'worker.lock'):
        with database(service) as db:
            db.execute("UPDATE operations SET state='running' WHERE operation_id=? AND state='queued'", (row['operation_id'],))
        frozen = json.loads(row['configuration'])
        runtime = frozen['runtime']
        request = ActionRequest(**json.loads(row['request']))
        path = Path(row['atlas_path'])
        previous = Path(row['previous_evidence']) if row['previous_evidence'] else None
        try:
            # AtlasLock itself resolves aliases. Check the persisted location first.
            if validate_atlas_path(service, path) != path:
                raise AtlasError('atlas_conflict', 'the queued repository or destination path changed', 409)
            with AtlasLock(path):
                binding = operation_project(service, request, path, frozen)
                attempt_service = replace(service, configuration=frozen['configuration'], runtime=(RuntimeConfig(
                    Path(runtime['runtime_root']), Path(runtime['manifest']) if runtime['manifest'] else None)
                    if runtime else None))
                if binding and 'project_binding' not in frozen:
                    # Legacy attempts recorded the destination and runtime but used the deployment database.
                    attempt_service.configuration = project_configuration(attempt_service, binding)
                context = ActionContext(attempt_service, request, path, evidence, attempt_service.configuration,
                                        row['operation_id'], previous, binding)
                handler = register_handlers(service).get(request.action)
                if handler is None: raise AtlasError('unsupported_action', request.action, 422)
                result = run_attempt(context, handler)
                if not isinstance(result, ActionResult): raise TypeError('handler must return ActionResult')
                result_json = encoded(asdict(result))
                (evidence/'result.json').write_text(result_json)
                with database(service) as db:
                    db.execute("UPDATE operations SET state='succeeded',result=?,error=NULL WHERE operation_id=?", (result_json, row['operation_id']))
        except Exception as error:
            details = {'code':getattr(error,'code','operation_failed'), 'message':str(error), 'evidence':str(evidence)}
            (evidence/'error.json').write_text(encoded(details))
            (evidence/'traceback.txt').write_text(traceback.format_exc())
            # A previous service version could already have queued an unsupported
            # restore resume. Its original bytes are still needed for inspection.
            interrupted = (request.action == 'archive.restore' and previous is not None
                           and details['code'] == 'unsafe_resume')
            with database(service) as db:
                db.execute("UPDATE operations SET state=?,error=? WHERE operation_id=?",
                           ('interrupted' if interrupted else 'failed', encoded(details), row['operation_id']))
            if interrupted:
                return
        # The state is durable before discarding bytes. An interrupted attempt
        # retains its original evidence until an operator reconciles the restore.
        if request.action == 'archive.restore':
            staged_restore_input(evidence).unlink(missing_ok=True)
