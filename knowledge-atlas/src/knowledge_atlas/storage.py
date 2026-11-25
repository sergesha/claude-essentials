"""On-disk boundaries shared by service startup and Atlas-owned stores."""
from contextlib import closing, contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
from stat import S_ISREG
import tempfile


FORMAT = 3


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


@contextmanager
def readonly_database(path, *, recover_after_validation=False):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'unsupported {path.name} storage')
    # A read-only connection to a WAL database can still create sidecars.
    # Atlas writes rollback-journal databases with a current user_version.
    with path.open('rb') as stream:
        header = stream.read(64)
    if (header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01'
            or int.from_bytes(header[60:64], 'big') != FORMAT):
        raise ValueError(f'unsupported {path.name} database format')

    uri = path.absolute().as_uri()
    with ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(uri + '?mode=ro', uri=True, timeout=10)))
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        try:
            db.execute('PRAGMA schema_version').fetchone()
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_READONLY_ROLLBACK:
                raise
        else:
            yield db
            return
        db.close()  # The original cannot be read until SQLite rolls it back.
        stack.enter_context(directory_lock(path.parent))
        journal = path.with_name(path.name + '-journal')
        if not journal.exists():
            # A concurrent validated opener completed ordinary recovery.
            with closing(sqlite3.connect(uri + '?mode=ro', uri=True, timeout=10)) as recovered:
                recovered.row_factory = sqlite3.Row
                recovered.execute('BEGIN')
                yield recovered
            return
        sources = (path, journal)
        identity = [_snapshot_identity(source) for source in sources]
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for source in sources:
            with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as input_file:
                with (scratch / source.name).open('xb') as output:
                    shutil.copyfileobj(input_file, output)
        if identity != [_snapshot_identity(source) for source in sources]:
            raise ValueError('database changed while copying interrupted transaction')
        recovered = stack.enter_context(closing(sqlite3.connect(scratch / path.name)))
        recovered.row_factory = sqlite3.Row
        recovered.execute('PRAGMA schema_version').fetchone()  # SQLite recovers the private copy.
        recovered.execute('PRAGMA query_only=ON')
        yield recovered
        if identity != [_snapshot_identity(source) for source in sources]:
            raise ValueError('database changed during interrupted transaction validation')
        if recover_after_validation:
            with closing(sqlite3.connect(uri + '?mode=rw', uri=True)) as current:
                current.execute('PRAGMA schema_version').fetchone()


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
