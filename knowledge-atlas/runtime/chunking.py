"""Lossless source slices with deterministic token budgets and bounded work."""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    input: str


def split(text, label, tokenizer, budget=256, *, max_chunks=256):
    if not 64 <= budget <= 2048:
        raise ValueError('CHUNK_MAX_TOKENS must be between 64 and 2048')
    tokenizer.no_truncation()
    tokenizer.no_padding()
    count = lambda value: len(tokenizer.encode(value).ids)
    label_limit = min(64, budget // 4)
    offsets = tokenizer.encode(label, add_special_tokens=False).offsets
    prefix = label[:offsets[min(len(offsets), label_limit)-1][1]] if offsets else ''
    while prefix and count(prefix + '\n') > label_limit:
        prefix = prefix[:-1]
    prefix = 'title: ' + (prefix or 'none') + ' | text: '
    if count(prefix) + 4 >= budget:
        raise ValueError('Label and prompt leave no content capacity')
    blocks, begin = [], 0
    for separator in re.finditer(r'\n\s*\n', text):
        if text[begin:separator.start()].strip():
            blocks.append((begin, separator.start()))
        begin = separator.end()
    if text[begin:].strip():
        blocks.append((begin, len(text)))
    spans = []
    for a, b in blocks:
        if count(prefix + text[a:b]) <= budget:
            if spans and count(prefix + text[spans[-1][0]:b]) <= budget:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
        else:
            offsets = tokenizer.encode(text[a:b], add_special_tokens=False).offsets
            start = 0
            while start < len(offsets):
                end = min(len(offsets), start + budget - count(prefix) - 4)
                while end > start and count(prefix + text[a+offsets[start][0]:a+offsets[end-1][1]]) > budget:
                    end -= 1
                if end <= start:
                    raise ValueError('A character cannot fit in the chunk budget')
                spans.append((a+offsets[start][0], a+offsets[end-1][1]))
                if len(spans) > max_chunks:
                    raise ValueError('Memory exceeds the supported number of chunks')
                if end == len(offsets):
                    break
                start = max(start+1, end-32)
        if len(spans) > max_chunks:
            raise ValueError('Memory exceeds the supported number of chunks')
    if not spans:
        # Whitespace-only input has no semantic content; do not send an unbounded blank string.
        if count(prefix + text) > budget:
            raise ValueError('Whitespace-only memory exceeds the chunk budget')
        spans = [(0, len(text))]
    return [Chunk(a, b, prefix + text[a:b]) for a, b in spans]
