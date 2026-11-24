"""Resolve the service's durable UUID, repository and projection bindings."""
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from .contracts import AtlasError, ServiceContext
from .core.git_store import GitStore, git
from .core.publication import Publications
from .core.reader import projection_receipt


@dataclass(frozen=True)
class ProjectBinding:
    atlas_id: str
    path: Path
    graph_database: str
    git_url: str | None


def project_id(value: str | None) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise AtlasError('invalid_request', 'atlas_id must be a UUID') from error


def repository_identity(path: Path) -> str:
    """Read the committed identity without materializing any included packages."""
    store = GitStore(path)
    try:
        if not (store.root / '.git').exists():
            raise ValueError('missing native Git repository')
        head = git(store.root, 'rev-parse', 'HEAD')
        package = json.loads(git(store.root, 'show', head + ':package.json'))
        manifest = json.loads(git(store.root, 'show', head + ':manifest.json'))
        identity = str(uuid.UUID(package['package_id']))
        manifest_identity = str(uuid.UUID(manifest['package_id']))
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise AtlasError('invalid_atlas', 'native atlas repository identity is unavailable', 422) from error
    if manifest_identity != identity:
        raise AtlasError('atlas_conflict', 'repository package and manifest UUIDs disagree', 409)
    return identity


def _fresh_database(atlas_id):
    return 'admin/atlas-' + uuid.UUID(atlas_id).hex


def _registry_binding(row):
    return ProjectBinding(row['atlas_id'], Path(row['path']),
                          row['graph_database'] or '', row['git_url'])


def list_projects(service: ServiceContext) -> list[ProjectBinding]:
    """Read the inventory, including unavailable registrations. Resolve a binding before use."""
    from .operations import database

    with database(service) as db:
        rows = db.execute('SELECT * FROM atlases ORDER BY path, atlas_id').fetchall()
    projects = []
    for row in rows:
        if row['graph_database'] is None:
            try:
                projects.append(resolve_project(service, row['atlas_id']))
                continue
            except AtlasError:
                # Retain the registration for diagnostics without inventing a binding.
                pass
        projects.append(_registry_binding(row))
    return projects


def resolve_project(service: ServiceContext, atlas_id: str) -> ProjectBinding:
    from .operations import database, validate_atlas_path

    atlas_id = project_id(atlas_id)
    with database(service) as db:
        row = db.execute('SELECT * FROM atlases WHERE atlas_id=?', (atlas_id,)).fetchone()
    if row is None:
        raise AtlasError('atlas_not_found', 'atlas UUID is not registered in this service', 404)
    path = Path(row['path'])
    try:
        resolved = validate_atlas_path(service, path)
        identity = repository_identity(path)
    except AtlasError as error:
        if error.code != 'invalid_atlas':
            raise
        raise AtlasError('atlas_conflict', 'the registered repository identity is unavailable', 409) from error
    if path != resolved or identity != atlas_id:
        raise AtlasError('atlas_conflict', 'the registered path or repository UUID disagrees with the binding', 409)
    if row['graph_database'] is None:
        return register_project(service, atlas_id, path)
    return _registry_binding(row)


def project_configuration(service: ServiceContext, binding: ProjectBinding) -> dict:
    graph = service.configuration.get('graph', {})
    if not isinstance(graph, dict):
        raise AtlasError('invalid_configuration', 'graph configuration must be an object', 422)
    return {**service.configuration, 'graph': {**graph, 'database': binding.graph_database}}


def graph_endpoint(url):
    address = urlsplit(url)
    return f'http://{address.hostname}:{address.port or 80}' + address.path.rstrip('/')


def verify_graph_owner(db, endpoint, database_name, atlas_id):
    reserved = db.execute('SELECT atlas_id FROM atlases WHERE graph_database=?', (database_name,)).fetchone()
    owner = db.execute('SELECT atlas_id FROM projection_owners WHERE endpoint=? AND database_name=?',
                       (endpoint, database_name)).fetchone()
    if any(row is not None and row['atlas_id'] != atlas_id for row in (reserved, owner)):
        raise AtlasError('graph_atlas_conflict', 'the graph database belongs to another atlas', 409)


def _legacy_database(db, store, atlas_id):
    try:
        pubs = Publications(store)
        ready = pubs.ready()
        if ready:
            name = projection_receipt(store, ready, pubs.read(ready))['database']
            if not isinstance(name, str) or not name:
                raise ValueError('missing projection database')
            return name
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise AtlasError('graph_atlas_conflict', 'the retained projection binding cannot be verified', 409) from error

    owned = db.execute('SELECT DISTINCT database_name FROM projection_owners WHERE atlas_id=?', (atlas_id,)).fetchall()
    if len(owned) > 1:
        raise AtlasError('graph_atlas_conflict', 'the atlas has multiple retained graph database bindings', 409)
    return owned[0]['database_name'] if owned else None


def _verified_location(service, atlas_id, path):
    from .operations import validate_atlas_path

    path = validate_atlas_path(service, path)
    if repository_identity(path) != atlas_id:
        raise AtlasError('atlas_conflict', 'repository UUID disagrees with the registration', 409)
    return path


def _registration_binding(db, service, atlas_id, path):
    row = db.execute('SELECT * FROM atlases WHERE atlas_id=?', (atlas_id,)).fetchone()
    if row and Path(row['path']) != path and Path(row['path']).exists():
        raise AtlasError('atlas_conflict', 'this UUID is already registered at another existing location', 409)
    if db.execute('SELECT 1 FROM atlases WHERE path=? AND atlas_id<>?', (str(path), atlas_id)).fetchone():
        raise AtlasError('atlas_conflict', 'this location is registered for another UUID', 409)
    graph = service.configuration.get('graph', {})
    endpoint = graph_endpoint(graph['url']) if isinstance(graph, dict) and graph.get('url') else None
    name = row['graph_database'] if row else None
    store = GitStore(path)
    if row and name is None:
        name = _legacy_database(db, store, atlas_id)
    name = name or _fresh_database(atlas_id)
    verify_graph_owner(db, endpoint, name, atlas_id)
    try:
        locator = git(store.root, 'config', '--local', '--get', 'remote.origin.url') or None
    except ValueError:
        locator = None
    return ProjectBinding(atlas_id, path, name, locator)


def register_project(service, atlas_id, path):
    from .operations import database

    atlas_id = project_id(atlas_id)
    path = _verified_location(service, atlas_id, path)
    with database(service) as db:
        db.execute('BEGIN IMMEDIATE')
        binding = _registration_binding(db, service, atlas_id, path)
        db.execute('''INSERT INTO atlases (atlas_id,path,graph_database,git_url) VALUES (?,?,?,?)
                      ON CONFLICT(atlas_id) DO UPDATE SET path=excluded.path,
                          graph_database=excluded.graph_database, git_url=excluded.git_url''',
                   (binding.atlas_id, str(binding.path), binding.graph_database, binding.git_url))
    return binding


def prepare_restored_project(service, path):
    """Select projection storage while keeping a failed restore out of the inventory."""
    from .operations import database, validate_atlas_path

    path = validate_atlas_path(service, path)
    identity = repository_identity(path)
    with database(service) as db:
        return _registration_binding(db, service, identity, path)
