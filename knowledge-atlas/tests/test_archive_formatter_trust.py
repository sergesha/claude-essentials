"""Archive hashes cannot authorize executable formatter code."""
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from knowledge_atlas.core.vector_chunks import chunks_for
from knowledge_atlas.runtime import _assets


def test_archive_cannot_self_authorize_formatter(tmp_path, monkeypatch):
    # Only the optional tokenizer dependency is substituted; formatter import is real.
    monkeypatch.setitem(sys.modules, 'tokenizers', SimpleNamespace(
        Tokenizer=SimpleNamespace(from_file=lambda path: None)))
    marker = tmp_path / 'executed'
    formatter = tmp_path / 'chunking.py'
    formatter.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    tokenizer = tmp_path / 'tokenizer.json'
    tokenizer.write_text('{}')
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    profile['profile']['formatter_sha256'] = hashlib.sha256(formatter.read_bytes()).hexdigest()
    profile['profile']['plugin_profile']['tokenizer_sha256'] = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    profile_path = tmp_path / 'profile.json'
    profile_path.write_text(json.dumps(profile))
    with pytest.raises(ValueError, match='qualified embedding profile'):
        chunks_for([], profile_path, tokenizer, formatter)
    assert not marker.exists()
