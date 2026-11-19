"""Feedback proposals and reviewed compilation updates over retained atlas data."""
import hashlib
import json
from pathlib import Path
import tempfile
from .git_store import git
from .graph_stage import records_for, key
from .applicability import impact
from .knowledge import propose_records
from .publication import commit_files


def record_case_outcome(store, content, record_id, raw_report, *, actor, recorded_at, base_publication=None, application_publication=None):
    from .publication import Publications
    applied_content = (Publications(store).read(application_publication)['content_commit']
                       if application_publication is not None else content)
    snapshot = store.read(applied_content)
    records = {r['@id']:r for r in records_for(snapshot, applied_content)}
    applied = records.get(record_id)
    if not applied or applied['kind'] not in ('lesson', 'statement', 'judgment'):
        raise ValueError('retained applied lesson, statement or judgment required')
    report = json.loads(raw_report)
    if not report.get('case_id') or not report.get('description') or not isinstance(report.get('context'), dict):
        raise ValueError('case ID, description and actual context required')
    record = {'id':'outcome-'+hashlib.sha256((report['case_id']+'\n'+raw_report).encode()).hexdigest()[:24],
              'kind':'outcome', 'text':report['description'], 'raw_report':raw_report,
              'report':report, 'source_time':report.get('observed_at'),
              'valid_time':report.get('valid_time'), 'applies_to':record_id,
              'application_publication':application_publication if application_publication is not None else base_publication,
              'provenance':[{'record_id':record_id, 'source_commit':applied_content, 'start':0,
                             'end':len(applied['text']), 'quote':applied['text'], 'role':'applied_claim'}]}
    candidate = propose_records(store, content, [record], actor=actor,
        method='Operator-submitted outcome; no inferred claim truth', reason='Propose review of application outcome',
        recorded_at=recorded_at, base_publication=base_publication)
    proposal = json.loads(git(store.root, 'show', candidate+':proposal.json'))
    proposal['revision_targets'] = [record_id]
    result = commit_files(store.root, candidate, {'proposal.json':proposal}, [candidate], 'Retain outcome revision target')
    git(store.root, 'update-ref', 'refs/poc9/proposals/'+result, result)
    return result


def revise_compilation(store, base, entries, wiki, extraction, evidence, *, actor, method, reason, recorded_at, base_publication=None, relation_model=None, relation_bundle=None):
    proposal = propose_records(store, base, entries, actor=actor, method=method, reason=reason,
                               recorded_at=recorded_at, base_publication=base_publication)
    proposed = store.read(proposal)
    # New native compilation produces real source/knowledge package commits.
    # Its root is merged into the proposal by actual Git parent edges, not by
    # checking out READY or treating a compilation as acceptance.
    compiled = store.snapshot(wiki, extraction, evidence, recorded_at=recorded_at,
        relation_model=relation_model, relation_bundle=relation_bundle,
        relation_previous=proposed.get('relations'))['commit']
    fresh = store.read(compiled)
    modules = {**proposed['manifest']['modules'], **fresh['manifest']['modules']}
    manifest = {**proposed['manifest'], 'modules':modules}
    metadata = json.loads(git(store.root,'show',proposal+':proposal.json'))
    metadata['compiled_snapshot'] = compiled
    metadata['wiki_impact'] = compilation_impact(proposed, fresh, proposal, compiled)
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp)/'gitmodules'
        config.write_bytes(git(store.root,'show',proposal+':.gitmodules',binary=True))
        for kind,mod in fresh['manifest']['modules'].items():
            git(store.root,'config','-f',str(config),'submodule.'+kind+'.path',mod['mount'])
            git(store.root,'config','-f',str(config),'submodule.'+kind+'.url',mod['url'])
        result = commit_files(store.root, proposal,
            {'manifest.json':manifest, '.gitmodules':config.read_bytes(),
             'knowledge-records.json':proposed['knowledge_records'], 'proposal.json':metadata,
             'relations.json':fresh['relations']},
            [proposal, compiled], 'Propose consistent Wiki and semantic revision',
            gitlinks={mod['mount']:mod['commit'] for mod in fresh['manifest']['modules'].values()})
    store.read(result)
    git(store.root, 'update-ref', 'refs/poc9/proposals/'+result, result)
    return result


def compilation_impact(previous, fresh, previous_commit, fresh_commit):
    # Existing citation/link edges identify pages to inspect, not false claims.
    # Use both revisions so removing a source/link cannot erase its impact.
    old_pages = {r['@id']: r for r in records_for(previous, previous_commit)
                 if r['@id'].startswith('Record/wiki-')}
    new_pages = {r['@id']: r for r in records_for(fresh, fresh_commit)
                 if r['@id'].startswith('Record/wiki-')}
    dependencies = {}
    for rid in old_pages.keys() | new_pages.keys():
        links = set(old_pages.get(rid, {}).get('links', [])) | set(new_pages.get(rid, {}).get('links', []))
        dependencies[rid] = {'depends_on': sorted(links)}
    changed_sources = {'Record/' + key('source', name)
                       for name in previous['sources'].keys() | fresh['sources'].keys()
                       if previous['sources'].get(name) != fresh['sources'].get(name)}
    wiki_impact = impact(dependencies, changed_sources)
    for rid, detail in wiki_impact.items():
        detail['reason'] = 'source_revision_changed'
        detail['content_change'] = ('added' if rid not in old_pages else
                                    'removed' if rid not in new_pages else
                                    'unchanged' if old_pages[rid]['text'] == new_pages[rid]['text'] else 'changed')
    return wiki_impact
