"""SQLite owns execution only. Handlers retain all canonical knowledge in Git."""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .contracts import ActionRequest, ActionResult, AtlasError, OperationRecord, ServiceContext
from .locks import is_locked
from .project_registry import project_configuration, project_id, register_project, resolve_project


PATH_ACTIONS = frozenset(('atlas.create', 'archive.restore'))

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
            CREATE TABLE IF NOT EXISTS atlases (
                atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, graph_database TEXT, git_url TEXT);
            CREATE TABLE IF NOT EXISTS projection_owners (
                endpoint TEXT NOT NULL, database_name TEXT NOT NULL, atlas_id TEXT NOT NULL,
                PRIMARY KEY (endpoint, database_name));
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
                request TEXT NOT NULL, configuration TEXT NOT NULL, atlas_path TEXT NOT NULL,
                state TEXT NOT NULL, attempt INTEGER NOT NULL, evidence TEXT NOT NULL,
                previous_evidence TEXT, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS resumes (request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS operation_uuid ON operations ({_UUID_KEY});
            CREATE INDEX IF NOT EXISTS resume_uuid ON resumes ({_UUID_KEY});
        ''')
        _migrate_registry(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _migrate_registry(connection):
    fields = {'graph_database', 'git_url'}
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(atlases)')}
    if fields - columns:
        # Another connection may migrate the registry while this one waits for the lock.
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(atlases)')}
            for field in sorted(fields - columns):
                connection.execute(f'ALTER TABLE atlases ADD COLUMN {field} TEXT')
    connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS atlas_graph_database ON atlases(graph_database)')


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
    if request.action not in PATH_ACTIONS:
        return resolve_project(context, request.atlas_id).path
    value = request.payload.get('path')
    if not isinstance(value, str) or not value:
        raise AtlasError('invalid_request', 'external atlas path required')
    return validate_atlas_path(context, Path(value).expanduser())


def register_atlas(context, atlas_id, path):
    return register_project(context, atlas_id, path)


def _binding_evidence(binding):
    return {**asdict(binding), 'path': str(binding.path)}


def _existing_ancestor(path):
    while not path.exists():
        path = path.parent
    return path


def _has_registered_destination(path, registered_paths):
    if path in registered_paths:
        return True
    if not registered_paths:
        return False

    ancestor = _existing_ancestor(path)
    requested = path.relative_to(ancestor)
    alternates = []
    for registered in registered_paths:
        registered_ancestor = _existing_ancestor(registered)
        if not ancestor.samefile(registered_ancestor):
            continue
        relative = registered.relative_to(registered_ancestor)
        if relative == requested:
            return True
        if len(relative.parts) == len(requested.parts):
            alternates.append(relative)

    if not alternates or not ancestor.is_dir():
        return False
    # Test the exact spellings together on the destination filesystem. Keep
    # all scratch entries under one private directory in the existing ancestor.
    with tempfile.TemporaryDirectory(dir=ancestor, prefix='.atlas-reservation-') as directory:
        probe = Path(directory)
        (probe / requested).mkdir(parents=True)
        return any((probe / alternate).exists() for alternate in alternates)


def _verify_create_destination(db, path):
    registered_paths = [Path(row['path']) for row in db.execute('SELECT path FROM atlases')]
    if _has_registered_destination(path, registered_paths):
        raise AtlasError('atlas_conflict', 'the create destination is reserved for a registered atlas', 409)


def operation_project(service, request, path, frozen):
    if request.action in PATH_ACTIONS:
        if validate_atlas_path(service, path) != path:
            raise AtlasError('atlas_conflict', 'the queued destination path changed', 409)
        if request.action == 'atlas.create':
            with database(service) as db:
                _verify_create_destination(db, path)
        return None
    try:
        binding = resolve_project(service, request.atlas_id)
    except AtlasError as error:
        if error.code == 'atlas_not_found':
            raise AtlasError('atlas_conflict', 'the queued atlas is no longer registered', 409) from error
        raise
    if binding.path != path:
        raise AtlasError('atlas_conflict', 'the queued atlas repository path changed', 409)
    expected = frozen.get('project_binding')
    if expected is not None:
        if binding.graph_database != expected['graph_database']:
            raise AtlasError('graph_atlas_conflict', 'the queued atlas graph database changed', 409)
        if _binding_evidence(binding) != expected:
            raise AtlasError('atlas_conflict', 'the queued atlas binding changed', 409)
    return binding


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


MAX_STAGED_ARCHIVE_BYTES = 1024**3


@contextmanager
def _archive_staging_lock(context):
    # Keep file copies outside SQLite transactions while serializing admission
    # and orphan cleanup across all service processes.
    with (context.service_root / 'archive-staging.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _retained_archive_bytes(context):
    """Called under the staging lock. Reconcile v0.3.1 copies and abandoned admissions."""
    total = 0
    with database(context) as db:
        for path in (context.service_root / 'attempts').glob('*/1/archive.zip'):
            attempt = path.parent
            operation = attempt.parent
            if attempt.is_symlink() or operation.is_symlink():
                continue
            try:
                identifier = str(uuid.UUID(operation.name))
            except ValueError:
                continue
            if identifier != operation.name:
                continue
            row = db.execute('SELECT state, request FROM operations WHERE operation_id=?', (identifier,)).fetchone()
            if row and json.loads(row['request'])['action'] != 'archive.restore':
                continue
            if row is None or row['state'] in ('succeeded', 'failed'):
                path.unlink(missing_ok=True)
            else:
                try:
                    total += path.lstat().st_size
                except FileNotFoundError:
                    pass  # The worker may have just finished and discarded the input.
    return total


def staged_restore_input(evidence):
    """All attempts of a restore share the input frozen by its first submission."""
    return evidence.parent / '1' / 'archive.zip'


def _stage_archive(context, request, evidence):
    from .core import archive

    source = external_archive_path(context, request.payload.get('archive'))
    try:
        with archive.regular_archive(source) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > archive.MAX_ARCHIVE_BYTES:
                raise ValueError('archive exceeds byte limit')
            if size + _retained_archive_bytes(context) > MAX_STAGED_ARCHIVE_BYTES:
                raise AtlasError('archive_staging_full', 'restore staging capacity is full', 507)
            archive.freeze_restore_input(stream, staged_restore_input(evidence), size)
    except (ValueError, OSError) as error:
        raise AtlasError('invalid_archive', 'restore requires a bounded archive with an archive.json manifest', 422) from error


def submit(context: ServiceContext, request: ActionRequest) -> OperationRecord:
    if request.request_id is None:
        raise AtlasError('invalid_request', 'mutations require request_id')
    from .actions import register_handlers
    if request.action not in register_handlers(context):
        raise AtlasError('unsupported_action', request.action, 422)
    atlas_id = request.atlas_id if request.action in PATH_ACTIONS else project_id(request.atlas_id)
    request = replace(request, request_id=str(uuid.UUID(request.request_id)), atlas_id=atlas_id)
    request_json = encoded(asdict(request))
    digest = hashlib.sha256(request_json.encode()).hexdigest()
    with database(context) as db:
        existing = _existing_submission(db, request, request_json, digest)
        if existing is not None:
            return existing
    binding = resolve_project(context, request.atlas_id) if request.action not in PATH_ACTIONS else None
    path = binding.path if binding else atlas_path(context, request)
    frozen = {'configuration': project_configuration(context, binding) if binding else context.configuration,
              'runtime': {'runtime_root': str(context.runtime.runtime_root.expanduser().resolve()),
                          'manifest': str(context.runtime.manifest.expanduser().resolve()) if context.runtime.manifest else None}
              if context.runtime else None}
    if binding:
        frozen['project_binding'] = _binding_evidence(binding)
    configuration = encoded(frozen)
    staging = _archive_staging_lock(context) if request.action == 'archive.restore' else nullcontext()
    with staging:
        if request.action == 'archive.restore':
            with database(context) as db:
                existing = _existing_submission(db, request, request_json, digest)
                if existing is not None:
                    return existing
        return _persist_submission(context, request, request_json, digest, path, configuration)


def _persist_submission(context, request, request_json, digest, path, configuration):
    identifier = str(uuid.uuid4())
    evidence = context.service_root / 'attempts' / identifier / '1'
    evidence.mkdir(parents=True, exist_ok=False)
    committed = False
    try:
        if request.action == 'archive.restore':
            _stage_archive(context, request, evidence)
        (evidence / 'input.json').write_text(encoded({'request':json.loads(request_json), **json.loads(configuration)}))
        with database(context) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = _existing_submission(db, request, request_json, digest)
            if existing is not None:
                return existing
            if request.action == 'atlas.create':
                _verify_create_destination(db, path)
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
    from .actions import register_recoveries
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
        request = json.loads(row['request'])
        action = request['action']
        pending_publication = (row['state'] == 'succeeded'
            and action == 'candidate.decide'
            and row['result'] is not None
            and json.loads(row['result']).get('data', {}).get('status') == 'accepted_pending_publication')
        if (row['state'] not in ('failed', 'interrupted') and not pending_publication) or is_locked(Path(row['evidence']) / 'worker.lock'):
            raise AtlasError('busy', 'resume requires an inactive failed, interrupted or publication-pending operation', 409)
        if action == 'archive.restore' and register_recoveries(context).get(action) is None:
            raise AtlasError('unsafe_resume', 'inspect the restore and submit a new request for an unused destination', 409)
        attempt = row['attempt'] + 1
        evidence = context.service_root / 'attempts' / operation_id / str(attempt)
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / 'input.json').write_text(encoded({'request':request, **json.loads(row['configuration'])}))
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
        if (self.context.service_root / 'attempts').exists():
            with _archive_staging_lock(self.context):
                _retained_archive_bytes(self.context)
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
