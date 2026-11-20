"""Controlled extraction passage benchmark for factual, modal and navigational rows."""
import copy
import json

import pytest
from knowledge_atlas.core.extraction_passage import extraction_passage
from knowledge_atlas.core.graph_stage import records_for
from knowledge_atlas.core.relations import resolve_relations

PAGE = """# Material notes

## Map

| Page | Type | Topic |
|---|---|---|
| [PETG](petg.md) | Entity | Material summary |
| [Settings](settings.md) | Concept | Temperatures |

## Settings

| Setting | Guidance | Details |
|---|---|---|
| Bed | 70–80 °C | [Print settings](settings.md) |
| Nozzle | 230–250 °C | Upper end above 100 mm/s |

## Related

- [Drying](drying.md)
- [[Material hub]]
- [Drying](drying.md) is required after two weeks of open storage.

## Map

| Period | Page |
|---|---|
| 2024 | [Historical settings](old.md) |

~~~
## Related
- [Example](example.md)
~~~
"""


def test_benchmark_excludes_navigation_and_retains_evidence():
    passage = extraction_passage(PAGE)
    navigation = ['Material summary', 'Concept | Temperatures', '- [Drying](drying.md)\n', '[[Material hub]]']
    evidence = ['70–80 °C', '230–250 °C', 'required after two weeks', '2024 | [Historical', '[Example](example.md)']
    assert all(text not in passage for text in navigation)
    assert all(text in passage for text in evidence)


@pytest.mark.parametrize('qualifiers', [{'mood':'requires'}, [['mood','requires']]])
def test_modal_assertion_keeps_native_mood_and_projects_modality(qualifiers):
    from test_projection_occurrences import snapshot

    data = snapshot(1)
    data['extraction']['passage_version'] = 1
    statement = data['extraction']['statements'][0]
    statement.update(quote='This material requires drying.', phrase='requires drying', qualifiers=qualifiers)
    original = copy.deepcopy(statement)
    data['relations'] = resolve_relations(data['extraction'])
    records = records_for(data, 'a' * 40)
    item = next(item for item in records if item['kind'] == 'statement')
    projected = json.loads(item['payload'])
    assert projected['modality'] == ['requires'] and projected['qualifiers'] == qualifiers
    assert item['text'] == original['quote'] and statement == original
    assert not any(item['kind'] == 'relation' for item in records)
    del data['extraction']['passage_version']
    data['relations'] = resolve_relations(data['extraction'])
    legacy = next(item for item in records_for(data, 'a' * 40) if item['kind'] == 'statement')
    assert json.loads(legacy['payload']) == original


def test_navigation_only_edit_reuses_native_request_and_keeps_wiki(monkeypatch, tmp_path):
    from knowledge_atlas.core import extract_stage

    binary = tmp_path / 'parser-identity'
    binary.write_bytes(b'controlled parser')
    monkeypatch.setenv('POC9_EXTRACT_BIN', str(binary))

    def native(mode, payload):
        if mode == 'prepare':
            return [{'role': 'user', 'content': payload['text']}]
        assert mode == 'parse'
        return {'skipped':0, 'truncated':False, 'statements':[], 'entities':[]}

    monkeypatch.setattr(extract_stage, 'native', native)
    calls = []

    def model(request):
        calls.append(request)
        return {'e':[], 's':[], 't':[], 'n':[]}

    profile = {'model': 'controlled'}
    old = {'notes.md': PAGE}
    first = extract_stage.extract_wiki(old, model, model_profile=profile)
    changed = {'notes.md': PAGE.replace('Material summary', 'Wiki overview')}
    second = extract_stage.extract_wiki(changed, model, model_profile=profile, previous=first, previous_wiki=old)
    assert 'Material summary' in old['notes.md'] and 'Wiki overview' in changed['notes.md']
    assert len(calls) == 1 and calls[0]['messages'][0]['content'] == extraction_passage(PAGE)
    assert first['model_calls'][0]['cache_reason'] == 'no_previous_page'
    assert second['model_calls'][0]['cache_reason'] == 'exact_match'
