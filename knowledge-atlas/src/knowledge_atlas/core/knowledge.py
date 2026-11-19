"""Explicit semantic proposals over retained Git data."""
import copy
from datetime import datetime
from .applicability import impact
from .git_store import git
from .graph_stage import records_for
from .publication import Publications, commit_files


def propose_records(store, base, entries, *, actor, method, reason, recorded_at, base_publication=None):
    if not actor or not method or not reason:
        raise ValueError('proposal author, method and reason required')
    if datetime.fromisoformat(recorded_at.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('recorded_at requires timezone')
    pubs = Publications(store)
    snapshot = store.read(base)
    if pubs.ready() != base_publication:
        raise ValueError('stale proposal: read current publication before editing')
    origin = pubs.read(base_publication)['content_commit'] if base_publication else snapshot['manifest'].get('proposal_base_content', base)
    if base_publication and origin != base:
        if (snapshot['manifest']['status'] != 'candidate'
                or snapshot['manifest'].get('proposal_base_content') != origin
                or snapshot['manifest'].get('proposal_base_publication') != base_publication):
            raise ValueError('proposal must derive from the selected published content')
        git(store.root, 'merge-base', '--is-ancestor', origin, base)
    retained = {r['id']: copy.deepcopy(r) for r in snapshot.get('knowledge_records', [])}
    previous_ids = set(retained)
    seen = set()
    for supplied in entries:
        entry = copy.deepcopy(supplied)
        rid = entry.get('id')
        if not isinstance(rid, str) or not rid or rid in seen:
            raise ValueError('unique nonempty semantic ID required')
        seen.add(rid)
        if entry.get('kind') not in ('statement', 'judgment', 'episode', 'outcome', 'interpretation', 'lesson', 'gap', 'wiki'):
            raise ValueError('explicit semantic kind required')
        if not isinstance(entry.get('text'), str) or not entry['text'].strip():
            raise ValueError('semantic text required')
        state = entry.get('state', 'active')
        if state not in ('active', 'withdrawn', 'needs_review'):
            raise ValueError('unsupported semantic state')
        if state == 'withdrawn' and not entry.get('reason'):
            raise ValueError('withdrawal reason required')
        if not entry.get('provenance'):
            raise ValueError('exact retained provenance required')
        for citation in entry['provenance']:
            source_commit = citation.get('source_commit', base)
            source_snapshot = snapshot if source_commit == base else store.read(source_commit)
            docs = {r['@id']: r for r in records_for(source_snapshot, source_commit)}
            source = docs.get(citation['record_id'])
            start, end = citation['start'], citation['end']
            if (source is None or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(source['text'])
                    or source['text'][start:end] != citation['quote']):
                raise ValueError('provenance quote/span does not match retained source')
            citation['source_commit'] = source_commit
        # Attribution belongs to the source; proposal.json records the curator.
        entry.setdefault('author', actor)
        entry.update(state=state, method=method, recorded_at=recorded_at)
        retained[rid] = entry
    for entry in retained.values():
        refs = [child for group in entry.get('support_sets', []) for child in group]
        refs += entry.get('depends_on', []) + entry.get('conflicts_with', [])
        if any(not isinstance(ref,str) or not ref for ref in refs):
            raise ValueError('nonempty semantic reference ID required')
    affected = impact(retained, seen)
    invalidated = impact(retained, seen & previous_ids)
    # Filling an unresolved reference does not revise its existing rule. The
    # candidate still needs review before the new ground can support it.
    # Changes to existing grounds keep the normal invalidation path; already
    # pending/withdrawn dependants are never automatically reaffirmed.
    for rid, detail in affected.items():
        if rid not in invalidated:
            detail.update(status='reevaluate', reason='required_ground_added')
    def unchanged_support(rid, path):
        if rid not in retained or rid in seen or rid in path:
            return False
        entry = retained[rid]
        if entry.get('state') != 'active':
            return False
        if any(dep in seen or dep in affected for dep in entry.get('depends_on', [])):
            return False
        groups = entry.get('support_sets', [])
        return not groups or any(group and all(unchanged_support(child, path | {rid}) for child in group)
                                 for group in groups)
    preserved = {rid for rid in affected if unchanged_support(rid, set())}
    for rid, detail in affected.items():
        if rid not in seen and retained[rid].get('state') != 'withdrawn':
            retained[rid]['impact'] = detail
            if rid in invalidated and rid not in preserved:
                retained[rid]['state'] = 'needs_review'
    manifest = {**snapshot['manifest'], 'status': 'candidate', 'accepted_at': None,
                'recorded_at': recorded_at, 'proposal_base_publication': base_publication,
                'proposal_base_content': origin}
    manifest.pop('acceptance', None)
    proposal = {'base_content': base, 'base_publication': base_publication, 'actor': actor,
                'method': method, 'reason': reason, 'recorded_at': recorded_at,
                'changed': sorted(seen), 'impact': affected}
    candidate = commit_files(store.root, base,
        {'manifest.json': manifest, 'knowledge-records.json': list(retained.values()), 'proposal.json': proposal},
        [base], 'Propose semantic records from retained atlas data')
    git(store.root, 'update-ref', 'refs/poc9/proposals/'+candidate, candidate)
    return candidate
