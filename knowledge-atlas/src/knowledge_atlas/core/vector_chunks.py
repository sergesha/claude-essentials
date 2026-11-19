"""Use the pinned plugin's formatter directly, preserving source character spans."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def chunks_for(records, profile_path, tokenizer_path, formatter_path):
    from tokenizers import Tokenizer
    from ..runtime import _assets
    trusted_assets = _assets()
    profile = json.loads(Path(profile_path).read_text())
    qualified = json.loads((trusted_assets / 'embedding-profile.json').read_text())
    if profile != qualified:
        raise ValueError('qualified embedding profile required')
    config = qualified['profile']
    for path, expected in ((tokenizer_path, config['plugin_profile']['tokenizer_sha256']),
                           (formatter_path, config['formatter_sha256'])):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('pinned tokenizer/formatter digest mismatch')
    spec = importlib.util.spec_from_file_location('poc9_pinned_chunking', trusted_assets / 'chunking.py')
    formatter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = formatter
    spec.loader.exec_module(formatter)
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    chunks = []
    for record in records:
        rid = record['@id'].split('/data/')[-1]
        for index, chunk in enumerate(formatter.split(record['text'], '', tokenizer,
                config['plugin_profile']['chunk_tokens'], max_chunks=config['plugin_profile']['max_chunks'])):
            text = record['text'][chunk.start:chunk.end]
            # The provider is the sole owner of the final model prompt.
            if chunk.input != 'title: none | text: ' + text:
                raise ValueError('formatter/provider final-input mismatch')
            identity = json.dumps([rid, profile['profile_id'], chunk.start, chunk.end, text], ensure_ascii=False)
            chunks.append({'@type': 'Chunk', 'key': hashlib.sha256(identity.encode()).hexdigest(),
                           'record': rid, 'ordinal': index, 'start': chunk.start, 'end': chunk.end,
                           'text': text, 'profile_id': profile['profile_id']})
    return chunks
