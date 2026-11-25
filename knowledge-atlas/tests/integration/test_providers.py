"""Bounded actual Wiki and native Utopia calls, independently per provider."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def test_native_wiki_extraction_and_alignment(runtime_configuration, request, tmp_path):
    import pytest
    provider = request.config.getoption('--provider')
    profile_path = request.config.getoption('--profile-config')
    if not provider or not profile_path:
        pytest.skip('native provider qualification requires explicit provider and profile configuration')
    profiles = json.loads(Path(profile_path).read_text())
    assert all(value['provider'] == provider for value in profiles.values())
    runtime = Path(runtime_configuration['runtime_root'])
    package = Path(__file__).resolve().parents[2]
    env = {**os.environ,'PYTHONPATH':str(package/'src'),
        'KNOWLEDGE_ATLAS_WIKI_ROOT':str(runtime/'wiki'),
        'POC9_EXTRACT_BIN':str(runtime/'extract/target/debug/poc9-extract')}
    if provider == 'claude':
        binary = shutil.which('claude'); assert binary
        private_bin = tmp_path/'bin'; private_bin.mkdir()
        (private_bin/'claude').symlink_to(Path(binary).resolve())
        env['PATH']=str(private_bin)+':/usr/bin:/bin:/usr/sbin:/sbin'
        assert shutil.which('codex',path=env['PATH']) is None
    script="import runpy,json,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['_exercise'](json.loads(sys.argv[2]),Path(sys.argv[3]))"
    with (tmp_path/'stdout.log').open('w') as out, (tmp_path/'stderr.log').open('w') as err:
        result = subprocess.run([str(runtime/'python/bin/python'),'-c',script,str(Path(__file__).resolve()),
            json.dumps(profiles),str(tmp_path)],env=env,stdout=out,stderr=err,timeout=900)
    assert result.returncode==0, (tmp_path/'stderr.log').read_text()[-6000:]
    evidence=json.loads((tmp_path/'qualification.json').read_text())
    assert evidence['wiki_pages'] > 0 and evidence['extracted_statements'] > 0
    assert evidence['alignment']['choices'][0]['key']=='supports'
    assert evidence['alignment']['choices'][0]['direction']=='reverse'


def _exercise(profiles, root):
    import asyncio
    import hashlib
    from knowledge_atlas.core.model_config import ConfiguredModels
    from knowledge_atlas.core.wiki_stage import WikiStage
    from knowledge_atlas.core.extract_stage import extract_wiki, native_call
    from knowledge_atlas.actions.sources import prepare_sources
    fixture = Path(__file__).resolve().parents[1]/'fixtures/fixtures/printlab/review.txt'
    text = fixture.read_text()
    excerpt = text[text.index('Trial P-31 failed'):text.index(' Lesson:')]
    sources = prepare_sources([
        {'source_id': 'printlab-p31', 'name': fixture.name, 'content': excerpt},
    ], [], {}, root/'input')['sources']
    (root/'cli-discovery.json').write_text(json.dumps({name:shutil.which(name) for name in ('claude','codex')}))
    models = ConfiguredModels(config=profiles,evidence=root/'model-calls')
    wiki=asyncio.run(WikiStage(root/'wiki').run_agent(sources, models.wiki,
        recorded_at='2026-11-13T00:00:00Z'))
    (root/'wiki.json').write_text(json.dumps(wiki,indent=2))
    assert wiki['sources'][0]['source_id'] == 'printlab-p31'
    assert wiki['references'], 'native Wiki must retain source references'
    # One real generated page bounds this provider smoke; lifecycle covers all pages.
    page=next(p for p in wiki['pages'] if 'P-31' in p['content'])
    name=page['path']+page['filename']
    extraction=extract_wiki({name:page['content']},models,model_profile=models.effective_profile('open'))
    (root/'extraction.json').write_text(json.dumps(extraction,indent=2))
    assert extraction['statements'] and not extraction['truncated'] and extraction['skipped']==0
    calls=[]
    alignment=native_call('phrase', {'items':[{'id':0,'phrase':'is supported by','examples':['Claim is supported by Report'],
        'quotes':['Report supports Claim'],'object_is_value':False}],
        'catalog':[{'id':'supports','kind':'relation','definition':'Evidence supports claim',
                    'subject_role':'evidence','object_role':'claim'}]},models,calls)
    (root/'alignment.json').write_text(json.dumps(calls,indent=2))
    (root/'qualification.json').write_text(json.dumps({'source_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'source_excerpt':excerpt,'wiki_pages':len(wiki['pages']),'extracted_statements':len(extraction['statements']),
        'alignment':alignment,'profiles':profiles},indent=2))
