"""Exact statement wire references; semantic extraction stays with Utopia/LLM."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile

CODEC_VERSION = 'statement-ref-v1'
COLLECTIONS = ('e', 's', 'n', 't')
STATEMENT_CODEC = '\n\nStatement transport encoding, applied AFTER the extraction rules:\nReturn the COMPLETE extraction for the CURRENT Passage, with all e/s/n/t arrays.\nBuild e, n and t afresh from the CURRENT Passage in their ordinary native array\nformat. No references are allowed in e, n or t. Prior classifications are not\nprovided and must not be inferred from the historical context.\nOnly within s, a whole unchanged statement may be encoded as {"ref": i}, its\nzero-based index in Previous statements.s. This copies that exact entire row.\nWrite new or changed statements in the ordinary eight-element array format.\nThe Git diff and Previous statements are context only, not current assertions.\nReassess each previous statement against the CURRENT Passage, including its\nparticipants, quotation, negation, conditions and time. Omitted statements are\nnot copied. Do not retain obsolete statements. Keep names in retained statements\nconsistent with the current e/n arrays. No partial patches or new keys. JSON only.\n'


def git_page_diff(previous_text, current_text):
    with tempfile.TemporaryDirectory(prefix='poc9-diff-') as tmp:
        directory = Path(tmp)
        (directory / 'old.md').write_text(previous_text, encoding='utf-8')
        (directory / 'new.md').write_text(current_text, encoding='utf-8')
        result = subprocess.run(
            ['git', 'diff', '--no-index', '--no-color', '--text', '--no-ext-diff',
             '--no-textconv', 'old.md', 'new.md'], cwd=directory,
            capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError('Wiki Git diff failed: ' + result.stderr[-2000:])
        return result.stdout


def prepare_update_request(native_request, previous_text, current_text, previous_raw):
    request = copy.deepcopy(native_request)
    request['messages'][0]['content'] += STATEMENT_CODEC
    context = {'Git diff (old to current, context only)': git_page_diff(previous_text, current_text),
               'Previous statements (context only)': {'s': previous_raw['s']}}
    request['messages'][1]['content'] = (json.dumps(context, ensure_ascii=False)
        + '\n\nCURRENT DOCUMENT:\n' + native_request['messages'][1]['content'])
    return request


def expand_statement_references(encoded, previous_raw):
    if not isinstance(encoded, dict) or set(encoded) != set(COLLECTIONS):
        raise ValueError('complete e/s/n/t object required')
    def reject_nested_references(value):
        if isinstance(value, dict):
            if 'ref' in value:
                raise ValueError('nested statement reference is not supported')
            for item in value.values():
                reject_nested_references(item)
        elif isinstance(value, list):
            for item in value:
                reject_nested_references(item)
    expanded = {}
    for key in COLLECTIONS:
        if not isinstance(encoded[key], list):
            raise ValueError('collection must be an array')
        expanded[key] = []
        for row in encoded[key]:
            if isinstance(row, dict):
                index = row.get('ref')
                if (key != 's' or set(row) != {'ref'} or type(index) is not int
                        or not 0 <= index < len(previous_raw['s'])):
                    raise ValueError('invalid whole-statement reference')
                row = previous_raw['s'][index]
            if not isinstance(row, list):
                raise ValueError('native array row required')
            reject_nested_references(row)
            expanded[key].append(copy.deepcopy(row))
    return expanded
