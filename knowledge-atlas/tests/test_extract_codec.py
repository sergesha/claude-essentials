"""Regression checks for the retained whole-statement transport decoder."""
import pytest
from knowledge_atlas.core.extract_codec import decode_reference_response, expand_statement_references

ROW = ['No heat below zero', 'Archive', 'temperature', None, 0, {'condition': 'dry'}, '2025', '2026']

def test_reference_preserves_complete_row_without_aliasing():
    previous = {'s': [ROW]}
    result = expand_statement_references({'e': [], 's': [{'ref': 0}], 'n': [], 't': []}, previous)
    assert result['s'] == [ROW]
    result['s'][0][5]['condition'] = 'changed'
    assert previous['s'][0][5] == {'condition': 'dry'}

@pytest.mark.parametrize('reference', [-1, 1, True, '0'])
def test_invalid_reference_is_explicit(reference):
    with pytest.raises(ValueError):
        expand_statement_references({'e': [], 's': [{'ref': reference}], 'n': [], 't': []}, {'s': [ROW]})

def test_nested_reference_is_not_silently_dropped():
    with pytest.raises(ValueError, match='nested'):
        expand_statement_references({'e': [], 's': [['quote', 'A', 'needs', {'ref': 0}]], 'n': [], 't': []}, {'s': [ROW]})

@pytest.mark.parametrize('raw', ['{"s":[]}', '```json\r\n{"s":[]}\r\n```', '```\n{"s":[]}\n```'])
def test_complete_reference_response(raw):
    assert decode_reference_response(raw) == {'s': []}


@pytest.mark.parametrize('raw', ['{} trailing', '```json\n{}\n``` trailing', '```json\n{} trailing\n```',
                                 '```json\n{}', '```python\n{}\n```', '{} {}', '{"s":[NaN]}'])
def test_reference_framing_consumes_only_complete_json(raw):
    with pytest.raises(ValueError):
        decode_reference_response(raw)
