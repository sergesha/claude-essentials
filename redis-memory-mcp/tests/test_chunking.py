"""Lossless bounded input and restart configuration, independent of model downloads."""
import importlib
from pathlib import Path
import sys
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors
sys.path.insert(0, str(Path(__file__).parents[1] / 'server'))

@pytest.fixture
def tokenizer():
    alphabet = sorted(pre_tokenizers.ByteLevel.alphabet())
    vocab = {s: i for i, s in enumerate(['[CLS]', '[SEP]'] + alphabet)}
    t = Tokenizer(models.BPE(vocab, []))
    t.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    t.post_processor = processors.TemplateProcessing(single='[CLS] $A [SEP]', special_tokens=[('[CLS]', 0), ('[SEP]', 1)])
    return t

@pytest.mark.parametrize('budget', [64, 256, 512])
def test_complete_unicode_and_code_coverage_with_bounded_label(tokenizer, budget, max_chunks=10000):
    chunking = importlib.import_module('chunking')
    text = ('Первая строка 日本語🙂\n\n' * 100) + '```python\n' + 'x = 1\n' * 200 + '```\nПоследний факт'
    chunks = chunking.split(text, 'Long label ' * 80, tokenizer, budget, max_chunks=10000)
    covered = bytearray(len(text))
    for chunk in chunks:
        assert len(tokenizer.encode(chunk.input).ids) <= budget
        assert text[chunk.start:chunk.end] in chunk.input
        covered[chunk.start:chunk.end] = b'\1' * (chunk.end - chunk.start)
    assert all(hit or c.isspace() for hit, c in zip(covered, text))
    assert chunks == chunking.split(text, 'Long label ' * 80, tokenizer, budget, max_chunks=10000)


def test_short_mixed_record_and_oversize_rejected(tokenizer):
    chunking = importlib.import_module('chunking')
    text = 'Intro\n\n```py\nx = 1\n\nprint(x)\n```\n\nEnd'
    chunks = chunking.split(text, 'Example', tokenizer, 128)
    assert len(chunks) == 1
    assert chunks[0].input.startswith('title: Example | text: ')
    assert text in chunks[0].input
    with pytest.raises(ValueError, match='chunks'):
        chunking.split('long text ' * 100, '', tokenizer, 64, max_chunks=2)


def test_startup_budget_changes_profile_and_rejects_invalid_values(monkeypatch):
    embedding = importlib.import_module('embedding')
    monkeypatch.setenv('CHUNK_MAX_TOKENS', '256')
    a = embedding.Profile.from_env()
    monkeypatch.setenv('CHUNK_MAX_TOKENS', '512')
    b = embedding.Profile.from_env()
    assert a.fingerprint != b.fingerprint
    assert a.chunk_tokens == 256 and b.chunk_tokens == 512
    for value in ['0', '63', '2049', 'oops']:
        monkeypatch.setenv('CHUNK_MAX_TOKENS', value)
        with pytest.raises(ValueError, match='CHUNK_MAX_TOKENS'):
            embedding.Profile.from_env()
