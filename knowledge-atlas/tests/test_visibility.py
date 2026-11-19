"""Visibility follows dependencies once, including cycles and pinned provenance."""
import json
from types import SimpleNamespace
import pytest
from knowledge_atlas.core.reader import AtlasReader


def reader(records, allowed=None):
    result = object.__new__(AtlasReader)
    result.policy = lambda: set(records) if allowed is None else allowed
    result.receipt = {'content_commit': 'selected'}
    return result


def test_dense_cycles_do_not_enumerate_paths():
    visits = []
    class Counted(dict):
        def __getitem__(self, key):
            visits.append(key)
            assert len(visits) <= 400, 'visibility enumerated paths instead of dependencies'
            return super().__getitem__(key)
    records = Counted({str(i): {'git_commit': 'current', 'links': [str(j) for j in range(12) if i != j]}
                       for i in range(12)})
    assert reader(records)._visible(records) == set(records)


@pytest.mark.parametrize('denied', ['missing', 'restricted'])
def test_denial_propagates_through_cycle_and_shared_dependencies(denied):
    records = {'a': {'git_commit': 'current', 'links': ['b', 'c']},
               'b': {'git_commit': 'current', 'links': ['a']},
               'c': {'git_commit': 'current', 'links': [denied]},
               'independent': {'git_commit': 'current', 'links': []},
               'restricted': {'git_commit': 'current', 'links': []}}
    allowed = set(records) - {'restricted'}
    assert reader(records, allowed)._visible(records) == {'independent'}


def test_provenance_uses_pinned_package_and_denies_unavailable_source(monkeypatch):
    citation = {'source_commit': 'old', 'source_package_id': 'package', 'record_id': 'source'}
    records = {'Record/knowledge-one': {'git_commit': 'current', 'links': [],
               'payload': json.dumps({'provenance': [citation]})},
               'source': {'git_commit': 'current', 'links': []}}
    r = reader(records)
    calls = []
    def read_package(package, commit, *, atlas_commit):
        calls.append((package, commit, atlas_commit))
        return [{'@id': 'source', 'git_commit': 'old', 'links': ['missing']}]
    r.store = SimpleNamespace(read_package=read_package)
    monkeypatch.setattr('knowledge_atlas.core.reader.records_for', lambda source, commit, namespace: source)
    assert r._visible(records) == {'source'}
    assert calls == [('package', 'old', 'selected')]
    r.store.read_package = lambda *a, **kw: (_ for _ in ()).throw(OSError('unavailable'))
    assert r._visible(records) == {'source'}


def test_pinned_provenance_cycle_rechecks_policy_and_document_scope(monkeypatch):
    citation = {'source_commit': 'same', 'record_id': 'source'}
    records = {'Record/knowledge-one': {'git_commit': 'same', 'links': ['peer'],
               'payload': json.dumps({'provenance': [citation]})},
               'peer': {'git_commit': 'same', 'links': ['Record/knowledge-one']},
               'source': {'git_commit': 'same', 'links': []},
               'historical-ground': {'git_commit': 'same', 'links': []}}
    historical = [{'@id': 'source', 'git_commit': 'same', 'links': ['historical-ground']},
                  {'@id': 'historical-ground', 'git_commit': 'same', 'links': []}]
    allowed = set(records)
    r = reader(records, allowed)
    r.store = SimpleNamespace(read=lambda commit: historical)
    monkeypatch.setattr('knowledge_atlas.core.reader.records_for', lambda source, commit, namespace: source)
    assert r._visible(records) == set(records)
    allowed.remove('historical-ground')
    assert r._visible(records) == {'source'}
    allowed.add('historical-ground')
    historical[0]['links'] = ['missing-in-historical-snapshot']
    assert r._visible(records) == {'source', 'historical-ground'}
