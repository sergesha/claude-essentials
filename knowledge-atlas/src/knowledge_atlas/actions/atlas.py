"""Atlas identity and status reuse PoC9's canonical Git package and refs."""
from datetime import datetime, timezone
import json
from ..contracts import ActionResult, AtlasError
from ..core.git_store import GitStore, _repo, _json, _commit, git
from ..core.publication import Publications
from ..operations import register_atlas, database


def create_atlas(context):
    path = context.atlas_path
    if path.exists():
        raise AtlasError('destination_exists', 'create requires an unused atlas path', 409)
    path.mkdir(parents=True, exist_ok=False)
    store = GitStore(path)
    identity = _repo(store.root)
    _json(store.root/'manifest.json', {'package_id':identity, 'status':'candidate', 'accepted_at':None,
           'recorded_at':datetime.now(timezone.utc).isoformat(), 'modules':{}})
    commit = _commit(store.root, 'Initialize empty atlas package')
    register_atlas(context.service, identity, path)
    return ActionResult(data={'atlas_id':identity,'path':str(path),'content_commit':commit},
                        canonical_effects={'created_atlas':identity})


def open_atlas(context):
    store = GitStore(context.atlas_path)
    try:
        commit = git(store.root, 'rev-parse', 'HEAD')
        snapshot = store.read(commit)
        identity = snapshot['manifest']['package_id']
    except (ValueError, KeyError, OSError) as error:
        raise AtlasError('invalid_atlas', 'native atlas package is unavailable', 422) from error
    register_atlas(context.service, identity, context.atlas_path)
    return ActionResult(data={'atlas_id':identity, 'path':str(context.atlas_path)}, publication=Publications(store).ready())


def atlas_status(context):
    store = GitStore(context.atlas_path)
    identity = json.loads(git(store.root, 'show', 'HEAD:package.json'))['package_id']
    def refs(prefix):
        return git(store.root, 'for-each-ref', '--format=%(objectname)', prefix).splitlines()
    with database(context.service) as db:
        active = [dict(row) for row in db.execute(
            "SELECT operation_id,state FROM operations WHERE atlas_path=? AND state IN ('queued','running')",
            (str(context.atlas_path),))]
    return ActionResult(data={'atlas_id':identity, 'path':str(context.atlas_path),
                             'active_operations':active, 'runtime_configured':context.service.runtime is not None,
                             'candidates':refs('refs/poc9/proposals/'), 'accepted_content':refs('refs/poc9/reviews/')},
                        publication=Publications(store).ready())
