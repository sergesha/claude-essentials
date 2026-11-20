"""Atlas identity and status reuse PoC9's canonical Git package and refs."""
from datetime import datetime, timezone
import json
from ..contracts import ActionResult, AtlasError
from ..core.git_store import GitStore, _repo, _json, _commit, git
from ..core.publication import Publications
from ..core.decision_receipts import decision_inventory, matching_decision_receipt, review_inventory
from ..operations import register_atlas, database


def create_atlas(context):
    path = context.atlas_path
    if path.exists():
        raise AtlasError('destination_exists', 'create requires an unused atlas path', 409)
    path.mkdir(parents=True, exist_ok=False)
    store = GitStore(path)
    identity = _repo(store.root)
    _json(store.root/'manifest.json', {'package_id':identity, 'status':'candidate', 'accepted_at':None, 'proposal_base_publication':None,
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
    pubs = Publications(store)
    ready = pubs.ready()
    candidates = refs('refs/poc9/proposals/')
    statuses = dict.fromkeys(candidates, 'pending_review')
    # A candidate may have multiple recorded decisions. An accepted effect is
    # durable even when a later decision comments on the same candidate.
    receipts = sorted((entry['receipt'] for entry in decision_inventory(store)),
                      key=lambda r: (datetime.fromisoformat(r['decided_at'].replace('Z', '+00:00')), r['decision_id']))
    for receipt in receipts:
        candidate = receipt['candidate']
        if candidate in statuses:
            statuses[candidate] = {'accept':'accepted', 'reject':'rejected', 'revise':'revised'}[receipt['action']]
    for _, review in review_inventory(store):
        if review.get('candidate_commit') in statuses:
            statuses[review['candidate_commit']] = 'accepted'
    return ActionResult(data={'atlas_id':identity, 'path':str(context.atlas_path),
                             'matching_decision_receipt':matching_decision_receipt(store, pubs.read(ready) if ready else None),
                             'candidate_statuses':statuses,
                             'active_operations':active, 'runtime_configured':context.service.runtime is not None,
                             'candidates':candidates, 'accepted_content':refs('refs/poc9/reviews/')},
                        publication=ready)
