"""SQLite owns execution only. Handlers retain all canonical knowledge in Git."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import uuid

from .contracts import ActionRequest, ActionResult, AtlasError, OperationRecord, ServiceContext
from .locks import is_locked


@contextmanager
def database(context):
    root = context.service_root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(root / 'operations.sqlite3', timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
                request TEXT NOT NULL, configuration TEXT NOT NULL, atlas_path TEXT NOT NULL,
                state TEXT NOT NULL, attempt INTEGER NOT NULL, evidence TEXT NOT NULL,
                previous_evidence TEXT, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS resumes (request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL);
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


def atlas_path(context, request):
    if request.action in ('atlas.create', 'atlas.open', 'archive.restore'):
        value = request.payload.get('path')
        if not isinstance(value, str) or not value:
            raise AtlasError('invalid_request', 'external atlas path required')
        path = Path(value).expanduser().resolve()
        if path.is_relative_to(Path(__file__).resolve().parents[2]):
            raise AtlasError('invalid_location', 'atlas data must live outside the plugin')
        return path
    with database(context) as db:
        row = db.execute('SELECT path FROM atlases WHERE atlas_id=?', (request.atlas_id,)).fetchone()
    if row is None:
        raise AtlasError('atlas_not_found', 'open the atlas first', 404)
    return Path(row['path'])


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


def submit(context: ServiceContext, request: ActionRequest) -> OperationRecord:
    if request.request_id is None:
        raise AtlasError('invalid_request', 'mutations require request_id')
    from .actions import register_handlers
    if request.action not in register_handlers(context):
        raise AtlasError('unsupported_action', request.action, 422)
    request_json = encoded(asdict(request))
    digest = hashlib.sha256(request_json.encode()).hexdigest()
    with database(context) as db:
        existing = db.execute('SELECT * FROM operations WHERE request_id=?', (request.request_id,)).fetchone()
        if existing:
            if existing['digest'] != digest: raise AtlasError('request_conflict', 'request_id already has another payload', 409)
            return _record(existing)
    path = atlas_path(context, request)
    configuration = encoded({'configuration': context.configuration, 'runtime':
        {'runtime_root': str(context.runtime.runtime_root.expanduser().resolve()),
         'manifest': str(context.runtime.manifest.expanduser().resolve()) if context.runtime.manifest else None}
        if context.runtime else None})
    identifier = str(uuid.uuid4())
    evidence = context.service_root / 'attempts' / identifier / '1'
    with database(context) as db:
        db.execute('BEGIN IMMEDIATE')
        # Concurrent submissions can have passed the first read together.
        existing = db.execute('SELECT * FROM operations WHERE request_id=?', (request.request_id,)).fetchone()
        if existing:
            if existing['digest'] != digest: raise AtlasError('request_conflict', 'request_id already has another payload', 409)
            return _record(existing)
        if db.execute('SELECT 1 FROM resumes WHERE request_id=?', (request.request_id,)).fetchone():
            raise AtlasError('request_conflict', 'request_id belongs to a resume', 409)
        evidence.mkdir(parents=True, exist_ok=False)
        if request.action == 'archive.restore':
            import shutil
            archive = request.payload.get('archive')
            if not isinstance(archive, str) or not archive:
                raise AtlasError('invalid_request', 'archive input path required')
            try:
                shutil.copyfile(Path(archive).expanduser().resolve(), evidence/'archive.zip')
            except OSError as error:
                raise AtlasError('invalid_archive', str(error), 422) from error
        (evidence / 'input.json').write_text(encoded({'request':json.loads(request_json), **json.loads(configuration)}))
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (identifier, request.request_id, digest, request_json, configuration, str(path), 'queued', 1, str(evidence), None, None, None))
        return _record(_row(db, identifier))


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
    inspect_operation(context, operation_id)
    with database(context) as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT operation_id FROM resumes WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if previous['operation_id'] != operation_id: raise AtlasError('request_conflict', 'resume UUID reused', 409)
            return _record(_row(db, operation_id))
        if db.execute('SELECT 1 FROM operations WHERE request_id=?', (request_id,)).fetchone():
            raise AtlasError('request_conflict', 'request UUID reused', 409)
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
