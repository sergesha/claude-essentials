"""On-disk boundaries shared by service startup and Atlas-owned stores."""
from contextlib import closing, contextmanager
from functools import lru_cache
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from stat import S_ISREG
import tempfile
import time


FORMAT = 3
_PROBE_PROGRAM = Path(__file__).with_name('_sqlite_probe.py')
_PROBE_PROTOCOL = b'atlas-sqlite-probe/2\n'


@contextmanager
def directory_lock(path, *, blocking=True):
    """Serialize first installation without writing a lock into the supplied state."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _snapshot_identity(path):
    stat = path.lstat()
    if not S_ISREG(stat.st_mode):
        raise ValueError(f'unsupported database file: {path.name}')
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _probe(*args):
    # A raw open/close in an SQLite host process can release the POSIX locks
    # of another connection to the same inode.
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-S', _PROBE_PROGRAM, *args],
                                stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as error:
        raise RuntimeError(f'SQLite inspection helper could not start: {error}') from error
    if result.stdout == _PROBE_PROTOCOL:
        if result.returncode == os.EX_NOINPUT:
            raise FileNotFoundError('database or rollback journal disappeared during inspection')
        if result.returncode == os.EX_DATAERR:
            raise ValueError(f'could not inspect SQLite input: {result.stderr.decode(errors="replace").strip()}')
    if result.returncode or not result.stdout.startswith(_PROBE_PROTOCOL):
        detail = result.stderr.decode(errors='replace').strip() or f'unexpected response (exit {result.returncode})'
        raise RuntimeError(f'SQLite inspection helper failed: {detail}')
    return result.stdout[len(_PROBE_PROTOCOL):]


@lru_cache(maxsize=1)
def _check_probe_revision(identity):
    # Exercise the installed header command before creating any supplied state.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'probe.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute(f'PRAGMA user_version={FORMAT}')
        expected = _snapshot_identity(path)
        try:
            if _verify_header(path, expected) == expected:
                return
        except (OSError, ValueError) as error:
            raise RuntimeError('SQLite inspection helper failed its header check') from error
        raise RuntimeError('SQLite inspection helper returned an invalid file fingerprint')


def check_probe_ready():
    try:
        identity = _snapshot_identity(_PROBE_PROGRAM)
    except (ValueError, OSError) as error:
        raise RuntimeError(f'SQLite inspection helper unavailable: {_PROBE_PROGRAM}') from error
    _check_probe_revision(identity)


@lru_cache(maxsize=1)
def _verify_header(path, identity):
    """Reuse only the most recently checked physical file revision during polling."""
    return _inspect_header(path, identity[:2])


def _inspect_header(path, original):
    deadline = time.monotonic() + 10
    while True:
        try:
            reply = _probe('header', path)
        except FileNotFoundError as error:
            raise ValueError(f'{path.name} disappeared during inspection') from error
        if reply != b'changed':
            break
        # Only retry a change across the helper's physical read of the header.
        if time.monotonic() >= deadline:
            raise ValueError(f'{path.name} kept changing during physical header inspection')
    opened, separator, header = reply.partition(b'\n')
    try:
        fingerprint = tuple(int(field) for field in opened.split(b':'))
    except ValueError as error:
        raise RuntimeError('SQLite inspection helper returned an invalid header reply') from error
    if not separator or len(fingerprint) != 5:
        raise RuntimeError('SQLite inspection helper returned an invalid header reply')
    if fingerprint[:2] != original:
        raise ValueError(f'{path.name} replaced during inspection')
    if (header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01'
            or int.from_bytes(header[60:64], 'big') != FORMAT):
        raise ValueError(f'unsupported {path.name} database format')
    return fingerprint


def _current_identity(path, original=None):
    try:
        identity = _snapshot_identity(path)
    except FileNotFoundError as error:
        raise ValueError(f'{path.name} disappeared during inspection') from error
    if original is not None and identity[:2] != original:
        raise ValueError(f'{path.name} replaced during inspection')
    return identity


def _journal_pair(path, original):
    identity = _current_identity(path, original)
    try:
        journal = _snapshot_identity(path.with_name(path.name + '-journal'))
    except FileNotFoundError:
        return None
    return identity, journal


@contextmanager
def _read_snapshot(path, original):
    check_probe_ready()
    uri = path.absolute().as_uri()
    for _ in range(4):
        inspected = _verify_header(path, _current_identity(path, original))
        if _current_identity(path, original) != inspected:
            # A commit or a journal-mode switch changed the file. Read the actual
            # format again. Writers can keep committing while this helper exits.
            _inspect_header(path, original)
            _current_identity(path, original)
        with closing(sqlite3.connect(uri + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            try:
                db.execute('PRAGMA schema_version').fetchone()
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise
            else:
                _current_identity(path, original)
                yield db, None
                return
        # An admitted handle may independently complete native recovery. Retry
        # capture if it changes the source pair before we have a stable copy.
        pair = _journal_pair(path, original)
        if pair is None:
            continue
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            try:
                if _probe('copy', path, scratch) != b'':
                    raise RuntimeError('SQLite inspection helper returned an invalid snapshot reply')
            except FileNotFoundError:
                _current_identity(path, original)
                continue
            if pair != _journal_pair(path, original):
                continue
            with closing(sqlite3.connect(scratch / path.name)) as recovered:
                recovered.row_factory = sqlite3.Row
                recovered.execute('PRAGMA schema_version').fetchone()
                recovered.execute('PRAGMA query_only=ON')
                # The stable pair is the read snapshot, regardless of later
                # commits or recovery by another already admitted connection.
                yield recovered, pair
                return
    raise ValueError(f'{path.name} changed repeatedly while taking a read snapshot')


@contextmanager
def readonly_database(path):
    original = _current_identity(path)[:2]
    with _read_snapshot(path, original) as (db, _):
        yield db


def validate_database(path, validate, *, recover=False):
    original = _current_identity(path)[:2]
    for _ in range(4):
        with _read_snapshot(path, original) as (db, pair):
            validate(db)
        if pair is None or not recover:
            return
        with directory_lock(path.parent):
            if pair != _journal_pair(path, original):
                continue  # Validate the new committed original before admitting a writer.
            with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True)) as current:
                current.execute('PRAGMA schema_version').fetchone()
            return
    raise ValueError(f'{path.name} changed repeatedly during recovery validation')


def verify_schema(db, schema):
    if db.execute('PRAGMA user_version').fetchone()[0] != FORMAT:
        raise ValueError('unsupported database format')
    actual = {row[0] for row in db.execute(
        "SELECT sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL")}
    if actual != set(schema):
        raise ValueError('unsupported database schema or constraints')


def init_stage(path):
    return path.with_name('.' + path.name + '.init-v3')


def initialize_database(path, schema, check):
    """Publish a complete schema from a private file under the owning directory lock."""
    check_probe_ready()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with directory_lock(path.parent):
        check()
        if path.exists():
            return
        stage = init_stage(path)
        stage.mkdir(mode=0o700, exist_ok=True)
        # A killed creator can leave partial private output. Only this call's
        # completed SQLite transaction is ever linked into the public location.
        with tempfile.NamedTemporaryFile(dir=stage, prefix='schema-') as pending:
            with closing(sqlite3.connect(pending.name)) as db, db:
                db.execute('BEGIN')
                for statement in schema:
                    db.execute(statement)
                db.execute(f'PRAGMA user_version={FORMAT}')
            os.link(pending.name, path)
        if not any(stage.iterdir()):
            stage.rmdir()


def check_init_stage(path):
    stage = init_stage(path)
    if stage.is_symlink() or (stage.exists() and
            (not stage.is_dir() or stage.stat().st_mode & 0o077 or
             any(not p.name.startswith('schema-') or p.is_symlink() or not p.is_file()
                 for p in stage.iterdir()))):
        raise ValueError(f'unsupported {path.name} initialization staging')


def check_graph_state(state):
    """Return True when the managed graph needs initialization or publication."""
    state = Path(state)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise ValueError('unsupported graph storage')
    if not state.exists():
        return True
    with directory_lock(state):
        return graph_needs_init(state)


def graph_needs_init(state):
    stage = state / '.graph-ready-v3'
    entries = {p.name for p in state.iterdir()}
    # Unpublished process-private workspaces can survive process death. Never
    # promote them: only a successful native initializer publishes the ready stage.
    for name in entries.copy():
        p = state / name
        if name.startswith('.graph-building-v3-') and p.is_dir() and not p.is_symlink() and not p.stat().st_mode & 0o077:
            entries.remove(name)
    if not entries:
        return True
    if stage.name in entries:
        if stage.is_symlink() or not stage.is_dir() or stage.stat().st_mode & 0o077:
            raise ValueError('unsupported graph initialization staging')
        if {p.name for p in stage.iterdir()} - {'atlas-format.json', 'admin.key', 'db', 'vectorlink'}:
            raise ValueError('unsupported contents in graph initialization staging')
        check_graph_bundle(stage, db=(stage / 'db') if (stage / 'db').exists() else state / 'db')
        if entries - {stage.name, 'atlas-format.json', 'admin.key', 'db', 'vectorlink'}:
            raise ValueError('unsupported graph initialization state')
        for name in ('atlas-format.json', 'admin.key'):
            target = state / name
            if name in entries and (target.is_symlink() or not target.samefile(stage / name)):
                raise ValueError('unsupported graph initialization owner')
        for name in ('db', 'vectorlink'):
            if name in entries and ((stage / name).exists() or
                                    not {'atlas-format.json', 'admin.key'} <= entries):
                raise ValueError('unsupported graph data conflicts with staging')
        for location in (state, stage):
            check_vectorlink_directory(location)
        return True
    if entries - {'db', 'atlas-format.json', 'admin.key', 'vectorlink'}:
        raise ValueError('unsupported graph state')
    check_vectorlink_directory(state)
    check_graph_bundle(state, db=state / 'db')
    return False


def check_vectorlink_directory(root):
    index = root / 'vectorlink'
    if index.is_symlink() or (index.exists() and not index.is_dir()):
        raise ValueError('unsupported graph vectorlink directory')


def check_graph_bundle(root, *, db):
    marker, key = root / 'atlas-format.json', root / 'admin.key'
    if (not db.is_dir() or db.is_symlink() or not any(db.iterdir()) or
            marker.is_symlink() or not marker.is_file() or key.is_symlink() or not key.is_file()
            or key.stat().st_mode & 0o077):
        raise ValueError('unsupported graph storage: require the current database, owner and private key')
    try:
        owner = json.loads(marker.read_text())
        valid = (owner == {'atlas_format': FORMAT} and type(owner['atlas_format']) is int
                 and re.fullmatch(r'[A-Za-z0-9_-]{43}\n?', key.read_text()) is not None)
    except (UnicodeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('unsupported graph storage owner or private key')
