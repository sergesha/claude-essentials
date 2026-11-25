"""Regenerate the retained printlab fixtures with the pinned Wiki and Utopia runtime.

Run with the runtime Python, KNOWLEDGE_ATLAS_WIKI_ROOT, POC9_EXTRACT_BIN and
PYTHONPATH=src from the knowledge-atlas package. Pass the output directory.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from knowledge_atlas.core.extract_stage import extract_wiki
from knowledge_atlas.core.wiki_stage import WikiStage


RECORDED = '2026-11-09T20:55:00Z'
REVIEW = 'In trial P-14, Luma produced fewer warped QL-7 brackets than Sora.'
REQUIREMENT = 'The lab requires technicians to record moisture before transferring the warping judgment.'
PAGES = [
    ('/wiki/', 'Overview', REVIEW + '[^1]\n\nSee [[concepts/conditional-warping]].\n\n[^1]: review.txt',
     ['overview', 'synthetic']),
    ('/wiki/concepts/', 'Conditional warping', REQUIREMENT + '[^1]\n\n[^1]: review.txt',
     ['conditions', 'warping']),
]


async def create_pages(request):
    async with stdio_client(StdioServerParameters(**request['mcp'])) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for path, title, content, tags in PAGES:
                result = await session.call_tool('create', {
                    'knowledge_base': request['knowledge_base'], 'path': path, 'title': title,
                    'content': content, 'tags': tags, 'date': RECORDED[:10],
                })
                if result.isError or 'Error:' in str(result):
                    raise RuntimeError(result)
    return {'status': 'completed', 'fixture': 'deterministic native stdio MCP create calls'}


def model(request):
    content = request['messages'][-1]['content']
    if REVIEW in content:
        return {'e': [['Luma', 'machine', 1], ['Sora', 'machine', 1]],
                's': [[REVIEW, 'Luma', 'produced fewer warped brackets than', 'Sora', None,
                       {'in': 'P-14'}, None, None]], 'n': [], 't': []}
    if REQUIREMENT in content:
        return {'e': [['technicians', 'role', 0]],
                's': [[REQUIREMENT, 'the lab', 'requires', 'technicians to record moisture', None,
                       {'mood': 'requires'}, None, None]], 'n': [], 't': []}
    raise AssertionError('unexpected fixture page')


def generate(output):
    fixture_input = Path('tests/fixtures/fixtures/printlab/review.txt')
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / fixture_input.name
        shutil.copyfile(fixture_input, source)
        wiki = asyncio.run(WikiStage(root / 'printlab').run_agent(
            [{'path': str(source), 'source_id': 'printlab-review', 'name': source.name}],
            create_pages, recorded_at=RECORDED))
    pages = {page['path'] + page['filename']: page['content'] for page in wiki['pages']}
    extraction = extract_wiki(pages, model)
    assert len(wiki['pages']) == 2 and wiki['references']
    assert len(extraction['statements']) == 2 and not extraction['skipped']
    output.mkdir(exist_ok=True)
    for name, data in [('wiki-current.json', wiki), ('extraction-current.json', extraction)]:
        with (output / name).open('w') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write('\n')


if __name__ == '__main__':
    generate(Path(sys.argv[1]))
