"""Service source IDs are stable; native Wiki filenames remain safe and unique."""
from pathlib import Path
import pytest
from knowledge_atlas.actions.sources import prepare_sources
from knowledge_atlas.contracts import AtlasError


def test_duplicate_filenames_keep_distinct_source_ids(tmp_path):
    items = [{'source_id':'one','name':'note.md','content':'First observation.'},
             {'source_id':'two','name':'note.md','content':'Second observation.'}]
    prepared = prepare_sources(items, [], {}, tmp_path/'inputs')
    assert len({Path(row['path']).name for row in prepared['sources']}) == 2
    assert [row['source_id'] for row in prepared['sources']] == ['one','two']
    assert [Path(row['path']).read_text() for row in prepared['sources']] == ['First observation.','Second observation.']
    updated = prepare_sources([{**items[0], 'name':'renamed.txt'}], [], {}, tmp_path/'renamed')
    assert Path(updated['sources'][0]['path']).name == Path(prepared['sources'][0]['path']).name


def test_omission_preserves_explicit_removal_reconciles(tmp_path):
    previous = {'sources':[{'source_id':'one','filename':'old-one.txt', 'recorded_at':'2026-11-12T00:00:00Z'},
                           {'source_id':'two','filename':'old-two.txt', 'recorded_at':'2026-11-12T00:00:00Z'}]}
    omission = prepare_sources([], [], previous, tmp_path/'omission')
    assert omission['remove_sources'] == []
    removal = prepare_sources([], ['one'], previous, tmp_path/'removal')
    assert removal['remove_sources'] == ['old-one.txt']
    assert previous['sources'][0]['source_id'] == 'one'
    with pytest.raises(AtlasError, match='unknown_source'):
        prepare_sources([], ['absent'], previous, tmp_path/'unknown')


def test_source_update_keeps_prior_internal_name_and_explicit_null(tmp_path):
    previous = {'sources':[{'source_id':'one','filename':'retained.txt', 'recorded_at':'2026-11-12T00:00:00Z'}]}
    prepared = prepare_sources([{'source_id':'one','name':'new.md','content':'Updated.', 'valid_to':None}], [], previous, tmp_path/'input')
    assert Path(prepared['sources'][0]['path']).name == 'retained.txt'
    assert prepared['sources'][0]['valid_to'] is None
    assert 'valid_from' not in prepared['sources'][0]


def test_source_submission_reports_missing_runtime_without_model_calls(tmp_path):
    from knowledge_atlas.actions.sources import submit_sources
    from knowledge_atlas.contracts import ActionContext, ActionRequest, ServiceContext
    context = ActionContext(ServiceContext(tmp_path/'service'), ActionRequest('sources.submit',
        payload={'sources':[{'source_id':'one','name':'note.md','content':'Text.'}],
                 'recorded_at':'2026-11-13T00:00:00Z'}), tmp_path/'atlas', tmp_path/'evidence', {})
    with pytest.raises(AtlasError) as error:
        submit_sources(context)
    assert error.value.code == 'runtime_unavailable'


def test_retained_filename_cannot_collide_with_new_source_id(tmp_path):
    import hashlib
    existing = hashlib.sha256(b'new').hexdigest()+'.txt'
    previous = {'sources':[{'source_id':'old','filename':existing, 'recorded_at':'2026-11-12T00:00:00Z'}]}
    with pytest.raises(AtlasError, match='source_name_conflict'):
        prepare_sources([{'source_id':'new','name':'note.txt','content':'new bytes'}], [], previous, tmp_path/'input')
    assert not (tmp_path/'input').exists()


def test_completed_wiki_reuse_cannot_ignore_source_removal(tmp_path):
    import asyncio
    import json
    from knowledge_atlas.core.ingest import ingest
    from knowledge_atlas.core.git_store import GitStore
    wiki = json.loads((Path(__file__).parent/'fixtures/evidence/wiki-current.json').read_text())
    removed = wiki['sources'][0]['filename']
    with pytest.raises(ValueError, match='removal'):
        asyncio.run(ingest(GitStore(tmp_path/'atlas'), tmp_path/'evidence', [],
            recorded_at='2026-11-13T00:00:00Z', model=lambda _:pytest.fail('incompatible reuse reached model'),
            resume_wiki=wiki, resume_extraction={'entities':[], 'statements':[]}, remove_sources=[removed]))
    assert not (tmp_path/'evidence/candidate.json').exists()
    assert json.loads((tmp_path/'evidence/execution.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('change', [
    {'decision_id':None}, {'action':None}, {'reason':''}, {'actor':''},
    {'action':'reuse','type_id':None}, {'candidate':'moving-branch'},
])
def test_type_decision_requires_explicit_valid_input_before_provider(tmp_path, monkeypatch, change):
    from knowledge_atlas.native_pipeline import execute_native
    from knowledge_atlas.contracts import ServiceContext
    from knowledge_atlas.core.git_store import git
    from test_project_registry import create_project
    path = tmp_path/'atlas'
    atlas_id = create_project(ServiceContext(tmp_path/'service'), path)
    before = git(path/'atlas', 'show-ref')
    def unexpected_model(**_):
        pytest.fail('invalid type decision reached a provider')
    monkeypatch.setattr('knowledge_atlas.core.model_config.ConfiguredModels', unexpected_model)
    decision = {'candidate':'a'*40,'actor':'Curator','decision_id':'decision-one','action':'accept',
                'reason':'Explicit reason','recorded_at':'2026-11-13T00:00:00Z', **change}
    with pytest.raises(AtlasError) as error:
        execute_native({'service_root':str(tmp_path/'service'), 'vector_cache':str(tmp_path/'service/vectors'), 'request':{'action':'type.decide','atlas_id':atlas_id,'payload':decision}, 'configuration':{},
            'atlas_path':str(tmp_path/'atlas'),'evidence':str(tmp_path/'evidence')})
    assert error.value.code == 'invalid_request'
    assert git(path/'atlas', 'show-ref') == before


def test_deciding_a_completed_type_question_returns_a_domain_error(tmp_path, monkeypatch):
    from knowledge_atlas.core import model_config
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.native_pipeline import execute_native
    from test_projection_occurrences import snapshot
    from knowledge_atlas.contracts import ServiceContext
    from test_project_registry import create_project
    path = tmp_path/'atlas'
    atlas_id = create_project(ServiceContext(tmp_path/'service'), path)
    selected = snapshot(1)
    selected['relations']['decisions'] = [{'id':'question','indices':[0],
        'extraction_digest':selected['relations']['extraction_digest']}]
    monkeypatch.setattr(GitStore, 'read', lambda self, oid: selected)
    monkeypatch.setattr(model_config, 'ConfiguredModels', lambda **kw: None)
    with pytest.raises(AtlasError) as caught:
        execute_native({'service_root':str(tmp_path/'service'), 'vector_cache':str(tmp_path/'service/vectors'), 'atlas_path': str(path), 'configuration':{}, 'evidence':str(tmp_path),
            'request':{'action':'type.decide', 'atlas_id':atlas_id, 'payload':{'candidate':'a'*40, 'decision_id':'question',
                'action':'reject', 'actor':'Fixture','reason':'Already handled','recorded_at':'2026-11-13T01:00:00Z'}}})
    assert caught.value.code == 'type_decision_unavailable' and caught.value.status == 422


@pytest.mark.parametrize('field', ['source_id', 'recorded_at'])
def test_prepare_sources_requires_retained_identity_and_time(tmp_path, field):
    import json

    previous = json.loads((Path(__file__).parent/'fixtures/evidence/wiki-current.json').read_text())
    source = previous['sources'][0]
    del source[field]
    destination = tmp_path/'input'
    with pytest.raises((AtlasError, ValueError), match=field):
        prepare_sources([], [], previous, destination)
    assert not destination.exists()
    assert field not in source
