"""SQLite owns execution only. Handlers retain all canonical knowledge in Git."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import multiprocessing
from pathlib import Path
import shutil
import sqlite3
import uuid

from .contracts import ActionRequest, ActionResult, AtlasError, OperationRecord, ServiceContext
from .locks import is_locked


# Python's uuid.UUID also accepts legacy upper-case, compact, braced and URN
# spellings. Index their equivalence without rewriting immutable request evidence.
_UUID_KEY = "replace(trim(replace(replace(lower(request_id),'urn:',''),'uuid:',''),'{}'),'-','')"


@contextmanager
def database(context):
    root = context.service_root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(root / 'operations.sqlite3', timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(f'''
            CREATE TABLE IF NOT EXISTS atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
                request TEXT NOT NULL, configuration TEXT NOT NULL, atlas_path TEXT NOT NULL,
                state TEXT NOT NULL, attempt INTEGER NOT NULL, evidence TEXT NOT NULL,
                previous_evidence TEXT, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS resumes (request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS operation_uuid ON operations ({_UUID_KEY});
            CREATE INDEX IF NOT EXISTS resume_uuid ON resumes ({_UUID_KEY});
        ''')
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AtlasError('invalid_request', 'finite JSON values required') from error


def _inside_atlas(path):
    return any((parent / 'atlas' / '.git').exists() and (parent / 'atlas' / 'package.json').is_file()
               for parent in path.parents)


def _registered_roots(context):
    with database(context) as db:
        return [Path(row['path']) for row in db.execute('SELECT path FROM atlases')]


def validate_atlas_path(context, path):
    path = Path(path).resolve()
    bound = context.configuration.get('atlas_path')
    if bound is not None and (not isinstance(bound, str) or not bound or path != Path(bound).expanduser().resolve()):
        raise AtlasError('invalid_location', 'this deployment is bound to a different atlas path', 422)
    if path.is_relative_to(Path(__file__).resolve().parents[2]) or path.is_relative_to(context.service_root):
        raise AtlasError('invalid_location', 'atlas data must live outside the plugin and service')
    if _inside_atlas(path) or any(root in path.parents for root in _registered_roots(context)):
        raise AtlasError('invalid_location', 'nested atlas paths are unsupported', 422)
    return path


def atlas_path(context, request):
    if request.action in ('atlas.create', 'atlas.open', 'archive.restore'):
        value = request.payload.get('path')
        if not isinstance(value, str) or not value:
            raise AtlasError('invalid_request', 'external atlas path required')
        return validate_atlas_path(context, Path(value).expanduser())
    with database(context) as db:
        row = db.execute('SELECT path FROM atlases WHERE atlas_id=?', (request.atlas_id,)).fetchone()
    if row is None:
        raise AtlasError('atlas_not_found', 'open the atlas first', 404)
    return validate_atlas_path(context, Path(row['path']))


def register_atlas(context, atlas_id, path):
    with database(context) as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT path FROM atlases WHERE atlas_id=?', (atlas_id,)).fetchone()
        if existing and Path(existing['path']).exists() and Path(existing['path']) != Path(path):
            raise AtlasError('atlas_conflict', 'this UUID is already open at another existing location', 409)
        db.execute('INSERT INTO atlases VALUES (?,?) ON CONFLICT(atlas_id) DO UPDATE SET path=excluded.path', (atlas_id, str(path)))


def _record(row):
    return OperationRecord(row['operation_id'], row['state'], row['attempt'], Path(row['evidence']),
                           ActionResult(**json.loads(row['result'])) if row['result'] else None,
                           json.loads(row['error']) if row['error'] else None)


def _row(db, identifier):
    row = db.execute('SELECT * FROM operations WHERE operation_id=?', (identifier,)).fetchone()
    if row is None: raise AtlasError('operation_not_found', identifier, 404)
    return row


def _request_owner(db, request_id):
    compact = uuid.UUID(request_id).hex
    owners = db.execute(f"""
        SELECT operation_id, 'operation' AS kind FROM operations WHERE {_UUID_KEY}=?
        UNION ALL
        SELECT operation_id, 'resume' AS kind FROM resumes WHERE {_UUID_KEY}=?
        LIMIT 2
    """, (compact, compact)).fetchall()
    if len(owners) > 1:
        raise AtlasError('request_conflict', 'legacy request UUID has multiple owners', 409)
    return owners[0] if owners else None


def _existing_submission(db, request, request_json, digest):
    owner = _request_owner(db, request.request_id)
    if owner is None:
        return None
    if owner['kind'] != 'operation':
        raise AtlasError('request_conflict', 'request_id belongs to a resume', 409)
    row = _row(db, owner['operation_id'])
    if row['digest'] != digest:
        # The legacy digest includes the original spelling of the UUID.
        stored = json.loads(row['request'])
        stored['request_id'] = request.request_id
        if encoded(stored) != request_json:
            raise AtlasError('request_conflict', 'request_id already has another payload', 409)
    return _record(row)


def _inside_private_root(source, roots):
    if any(source.is_relative_to(root) for root in roots):
        return True
    # On case-insensitive filesystems a different spelling can name the same
    # directory. Compare the directory identities as well as normalized paths.
    private = set()
    for root in roots:
        try:
            info = root.stat()
        except FileNotFoundError:
            continue
        private.add((info.st_dev, info.st_ino))
    for parent in source.parents:
        info = parent.stat()
        if (info.st_dev, info.st_ino) in private:
            return True
    return False


def external_archive_path(context, value):
    if not isinstance(value, str) or not value:
        raise AtlasError('invalid_request', 'archive input path required')
    try:
        source = Path(value).expanduser().resolve(strict=True)
    except (ValueError, OSError, RuntimeError) as error:
        raise AtlasError('invalid_archive', 'archive input path is unavailable', 422) from error
    roots = [context.service_root, *_registered_roots(context)]
    bound = context.configuration.get('atlas_path')
    if bound:
        roots.append(Path(bound).expanduser().resolve())
    if _inside_atlas(source) or _inside_private_root(source, roots):
        raise AtlasError('invalid_archive', 'archive input must be outside service and atlas data', 422)
    return source


def _stage_archive(context, request, evidence):
    from .core import archive

    source = external_archive_path(context, request.payload.get('archive'))
    try:
        with archive.regular_archive(source) as stream, (evidence / 'archive.zip').open('xb') as output:
            archive.copy_bounded(stream, output, archive.MAX_ARCHIVE_BYTES)
    except (ValueError, OSError) as error:
        raise AtlasError('invalid_archive', 'restore requires a regular file within the archive limit', 422) from error


def submit(context: ServiceContext, request: ActionRequest) -> OperationRecord:
    if request.request_id is None:
        raise AtlasError('invalid_request', 'mutations require request_id')
    from .actions import register_handlers
    if request.action not in register_handlers(context):
        raise AtlasError('unsupported_action', request.action, 422)
    request = replace(request, request_id=str(uuid.UUID(request.request_id)))
    request_json = encoded(asdict(request))
    digest = hashlib.sha256(request_json.encode()).hexdigest()
    with database(context) as db:
        existing = _existing_submission(db, request, request_json, digest)
        if existing is not None:
            return existing
    path = atlas_path(context, request)
    configuration = encoded({'configuration': context.configuration, 'runtime':
        {'runtime_root': str(context.runtime.runtime_root.expanduser().resolve()),
         'manifest': str(context.runtime.manifest.expanduser().resolve()) if context.runtime.manifest else None}
        if context.runtime else None})
    identifier = str(uuid.uuid4())
    evidence = context.service_root / 'attempts' / identifier / '1'
    evidence.mkdir(parents=True, exist_ok=False)
    committed = False
    try:
        if request.action == 'archive.restore':
            try:
                _stage_archive(context, request, evidence)
            except AtlasError:
                # Another caller might have finished while our input disappeared.
                with database(context) as db:
                    existing = _existing_submission(db, request, request_json, digest)
                if existing is not None:
                    return existing
                raise
        (evidence / 'input.json').write_text(encoded({'request':json.loads(request_json), **json.loads(configuration)}))
        with database(context) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = _existing_submission(db, request, request_json, digest)
            if existing is not None:
                return existing
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, request.request_id, digest, request_json, configuration, str(path), 'queued', 1, str(evidence), None, None, None))
            result = _record(_row(db, identifier))
        committed = True
        return result
    finally:
        if not committed:
            shutil.rmtree(evidence.parent)


def inspect_operation(context, operation_id):
    with database(context) as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, operation_id)
        if row['state'] == 'running' and not is_locked(Path(row['evidence']) / 'worker.lock'):
            db.execute("UPDATE operations SET state='interrupted',error=? WHERE operation_id=?", (
                encoded({'code':'interrupted','message':'worker lock is inactive; canonical effects must be reconciled'}), operation_id))
            row = _row(db, operation_id)
        return _record(row)


def resume(context, operation_id, request_id):
    if request_id is None:
        raise AtlasError('invalid_request', 'resume requires a fresh request UUID')
    ActionRequest('operation.resume', request_id=request_id)
    request_id = str(uuid.UUID(request_id))
    inspect_operation(context, operation_id)
    with database(context) as db:
        db.execute('BEGIN IMMEDIATE')
        owner = _request_owner(db, request_id)
        if owner is not None:
            if owner['kind'] != 'resume' or owner['operation_id'] != operation_id:
                raise AtlasError('request_conflict', 'request UUID reused', 409)
            return _record(_row(db, operation_id))
        row = _row(db, operation_id)
        pending_publication = (row['state'] == 'succeeded'
            and json.loads(row['request'])['action'] == 'candidate.decide'
            and row['result'] is not None
            and json.loads(row['result']).get('data', {}).get('status') == 'accepted_pending_publication')
        if (row['state'] not in ('failed', 'interrupted') and not pending_publication) or is_locked(Path(row['evidence']) / 'worker.lock'):
            raise AtlasError('busy', 'resume requires an inactive failed, interrupted or publication-pending operation', 409)
        attempt = row['attempt'] + 1
        evidence = context.service_root / 'attempts' / operation_id / str(attempt)
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / 'input.json').write_text(encoded({'request':json.loads(row['request']), **json.loads(row['configuration'])}))
        db.execute('INSERT INTO resumes VALUES (?,?)', (request_id, operation_id))
        db.execute("UPDATE operations SET state='queued',attempt=?,previous_evidence=evidence,evidence=?,result=NULL,error=NULL WHERE operation_id=?", (attempt, str(evidence), operation_id))
        return _record(_row(db, operation_id))


def dispatch(context):
    """Launch at most one active mutation worker; the service calls again for queued work."""
    from .worker import run_next
    process = multiprocessing.get_context('spawn').Process(target=run_next, args=(context,))
    process.start()
    return process


class Dispatcher:
    """Service-owned polling loop; child work is independent of request connections."""
    def __init__(self, context):
        import threading
        self.context = context
        self.stopping = threading.Event()
        self.thread = None
        self.process = None

    def start(self):
        import threading
        if self.thread is not None and self.thread.is_alive(): return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, name='atlas-dispatcher', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            if self.process is not None and not self.process.is_alive():
                self.process.join(); self.process = None
            with database(self.context) as db:
                running = [row[0] for row in db.execute("SELECT operation_id FROM operations WHERE state='running'")]
                queued = db.execute("SELECT 1 FROM operations WHERE state='queued' LIMIT 1").fetchone()
            for identifier in running:
                inspect_operation(self.context, identifier)
            if queued and self.process is None and not is_locked(self.context.service_root/'writer.lock'):
                self.process = dispatch(self.context)
            self.stopping.wait(.05)

    def stop(self, *, wait=False):
        import time
        while True:
            with database(self.context) as db:
                active = [row[0] for row in db.execute("SELECT operation_id FROM operations WHERE state IN ('queued','running')")]
            if not active: break
            if not wait: raise AtlasError('busy', 'active operations: ' + ', '.join(active), 409)
            time.sleep(.05)
        self.stopping.set()
        if self.thread is not None: self.thread.join()
        if self.process is not None: self.process.join()
