"""Packaged MCP lifecycle on a real native runtime and explicit provider profile."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import uuid


def test_packaged_lifecycle(request, runtime_configuration, tmp_path):
    import pytest
    host=request.config.getoption('--host')
    profile_path=request.config.getoption('--profile-config')
    if not host or not profile_path:
        pytest.skip('complete lifecycle requires --host and --profile-config')
    profiles=json.loads(Path(profile_path).read_text())
    assert all(p['provider']==host for p in profiles.values())
    native=request.getfixturevalue('native_graph')
    package=Path(__file__).resolve().parents[2]
    config={'service_root':str(tmp_path/'service'),'runtime':{'runtime_root':str(native['runtime'])},
        'configuration':{'profiles':profiles,'graph':{'url':native['graph'].url,'database':native['graph'].database}}}
    config_path=tmp_path/'service.json';config_path.write_text(json.dumps(config))
    from knowledge_atlas.client import AtlasClient
    try:
        asyncio.run(_cycle(package,config_path,native,tmp_path,host,profiles))
    finally:
        for service_root in (tmp_path/'service',tmp_path/'restored-service'):
            if (service_root/'discovery.json').exists():
                from knowledge_atlas.contracts import AtlasError
                deadline=time.monotonic()+30
                while True:
                    try:
                        AtlasClient(service_root).stop();break
                    except AtlasError as error:
                        if error.code!='busy' or time.monotonic()>deadline:
                            (service_root/'cleanup-error.txt').write_text(str(error));break
                        time.sleep(.2)


async def _cycle(package, config_path, native, root, host, profiles):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.graph_stage import records_for, key
    from knowledge_atlas.core.publication import Publications
    # Retained reviewed package inputs: source fixtures, not a second service.
    child=GitStore(root/'child')
    wiki=json.loads((package/'tests/fixtures/evidence/wiki-initial-final.json').read_text())
    extraction=json.loads((package/'tests/fixtures/evidence/extractor-output.json').read_text())
    profile=json.loads((package/'runtime/embedding-profile.json').read_text())
    child_pins=[]
    for index in range(2):
        if index: wiki['pages'][0]['content']+='\nRetained fixture revision for pin qualification.\n'
        candidate=child.snapshot(wiki,extraction,{},recorded_at='2026-11-13T00:00:00Z')['commit']
        child_pins.append(Publications(child).review(candidate,{'decision':'accept','kind':'synthetic',
            'actor':'Package fixture','reason':'Technical pin fixture only','accepted_at':'2026-11-13T00:00:00Z',
            'base_publication':None},profile))
    child_id=child.read(child_pins[0])['manifest']['package_id']
    env={**os.environ,'KNOWLEDGE_ATLAS_CONFIG':str(config_path),'KNOWLEDGE_ATLAS_PYTHON':sys.executable}
    params=StdioServerParameters(command='sh',args=[str(package/'scripts/launch.sh')],env=env)
    transcript=[];atlas_id=None;ready=None;sequence=0
    def now():
        nonlocal sequence
        sequence+=1
        return f'2026-11-13T02:{sequence//60:02d}:{sequence%60:02d}Z'
    async with stdio_client(params) as (reader,writer):
      async with ClientSession(reader,writer) as session:
        await session.initialize()
        tools=await session.list_tools()
        (root/'mcp-tools.json').write_text(json.dumps([t.model_dump(mode='json') for t in tools.tools],indent=2))
        async def call(action,payload,*,mutation=False,**selection):
            arguments={'payload':payload,**({'atlas_id':atlas_id} if atlas_id else {}),**selection}
            if mutation:arguments.update(request_id=str(uuid.uuid4()),expected_publication=ready)
            response=await session.call_tool('atlas_'+action.replace('.','_'),arguments)
            value=response.structuredContent
            transcript.append({'action':action,'arguments':arguments,'response':value})
            (root/'mcp-actions.json').write_text(json.dumps(transcript,indent=2))
            assert not response.isError, value
            if not mutation:return value
            operation=value;deadline=time.monotonic()+1200
            while operation['state'] in ('queued','running'):
                assert time.monotonic()<deadline, 'operation deadline; retained work must be inspected'
                await asyncio.sleep(.5)
                polled=await session.call_tool('atlas_operation_read',{'payload':{'operation_id':operation['operation_id']}})
                operation=polled.structuredContent
                if polled.isError:
                    transcript[-1]['completed_operation']=operation
                    (root/'mcp-actions.json').write_text(json.dumps(transcript,indent=2))
                assert not polled.isError, operation
            transcript[-1]['completed_operation']=operation
            (root/'mcp-actions.json').write_text(json.dumps(transcript,indent=2))
            assert operation['state']=='succeeded', operation
            return operation['result']
        async def accept(candidate):
            nonlocal ready
            await call('candidate.inspect',{'candidate':candidate})
            result=await call('candidate.decide',{'candidate':candidate,'action':'accept','actor':'Lifecycle qualification',
                'kind':'synthetic','reason':'Explicit technical exercise, not human acceptance','decided_at':now()},mutation=True)
            assert result['data']['status']=='published', result
            ready=result['publication'];return result
        created=await call('atlas.create',{'path':str(native['atlas'])},mutation=True)
        atlas_id=created['data']['atlas_id']
        raw=(package/'tests/fixtures/fixtures/printlab/review.txt').read_text()
        distinct=(package/'tests/fixtures/lifecycle/archive.txt').read_text()
        sources=[{'source_id':'printlab','name':'review.txt','content':raw},
                 {'source_id':'archive','name':'review.txt','content':distinct}]
        proposal=await call('sources.submit',{'sources':sources,'recorded_at':now()},mutation=True)
        candidate=proposal['data']['candidate']
        inspected=await call('candidate.inspect',{'candidate':candidate})
        snapshot=inspected['data']['snapshot']
        assert len(snapshot['wiki']['sources'])==2
        assert snapshot['wiki']['references'] and snapshot['extraction']['statements']
        (root/'initial-inspection.json').write_text(json.dumps(inspected,indent=2))
        # Native catalogue decisions remain distinct from knowledge acceptance.
        pending_types={row.get('decision_id') for row in snapshot.get('relations',{}).get('relations',[])
                       if row['status']=='needs_human'}
        for question in snapshot.get('relations',{}).get('decisions',[]):
            if question['id'] in pending_types:
                typed=await call('type.decide',{'candidate':candidate,'decision_id':question['id'],'action':'reject',
                    'actor':'Synthetic qualification','reason':'Retain unresolved distinction for explicit review',
                    'recorded_at':now()},mutation=True)
                candidate=typed['data']['candidate']
        first=await accept(candidate);first_publication=ready
        first_content=first['data']['accepted_content']
        factual = await call('question.ask', {
            'question': 'Which trial failed when humidity was not measured?', 'count': 10},
            publication=first_publication)
        assert factual['data']['status'] == 'answered'
        assert factual['data']['coverage'] == 'answered'
        assert factual['data']['citations']
        assert re.search(r'\bP\W*31\b', factual['data']['answer'], re.IGNORECASE)
        missing = await call('question.ask', {
            'question': 'What tensile strength in MPa did trial P-31 measure for the QL-7 brackets?',
            'count': 10}, publication=first_publication)
        assert missing['data']['bounds']['retrieved'] > 0
        assert missing['data']['status'] == 'insufficient_evidence'
        assert missing['data']['coverage'] == 'insufficient'
        # Adapt only the source locator of the retained PoC9 semantic fixture.
        import runpy
        records_factory=runpy.run_path(str(package/'tests/retained_semantics.py'))['proposal_records']
        entries=records_factory({'sources':{'review.txt':raw.encode()}})
        store=GitStore(native['atlas'])
        source=next(s for s in store.read(first_content)['wiki']['sources'] if s['source_id']=='printlab')
        for entry in entries:
            for citation in entry['provenance']:
                citation['record_id']='Record/'+key('source',source['filename'])
        proposed=await call('knowledge.revise',{'record_id':'Record/'+key('source',source['filename']),
            'entries':entries,'actor':'Synthetic qualification','kind':'synthetic',
            'reason':'Retained PoC9 conditional grounds fixture','recorded_at':now()},mutation=True)
        revised=await call('candidate.revise',{'candidate':proposed['data']['candidate'],'entries':[],
            'actor':'Synthetic qualification','kind':'synthetic',
            'reason':'Confirm retained fixture before explicit acceptance','decided_at':now()},mutation=True)
        await accept(revised['data']['candidate'])
        semantic_publication=ready
        content=Publications(store).read(ready)['content_commit']
        lesson=next(r['@id'] for r in records_for(store.read(content),content) if r['kind']=='lesson')
        condition={'feedstock':'dried','enclosure':'closed','ambient_c':22}
        assessment=await call('knowledge.apply',{'record_id':lesson,'context':condition})
        assert assessment['data']['status']=='supported'
        assert set(assessment['data']['assessment']['independent_episodes'])=={'P-14','P-22'}
        unknown=await call('knowledge.apply',{'record_id':lesson,'context':condition},valid_at='2026-11-13T03:00:00Z')
        assert 'valid_time' in unknown['data']['unknowns']
        found=await call('search',{'query':'conditional warping','mode':'hybrid','count':1,'eligible':[lesson]})
        assert found['data']['hits'][0]['record']['@id']==lesson
        await call('evidence.read',{'record_id':lesson,'expand':True})
        # Source omission retains printlab; the distinct domain is updated natively.
        updated=await call('sources.submit',{'sources':[dict(sources[1],content=distinct+' Audit A-8 recorded one missing folio; its cause is unknown.')],
            'recorded_at':now()},mutation=True)
        await accept(updated['data']['candidate'])
        assert len(store.read(Publications(store).read(ready)['content_commit'])['wiki']['sources'])==2
        feedback=await call('feedback.record',{'record_id':lesson,'applied_publication':semantic_publication,
            'raw_report':json.dumps({'case_id':'qualification-outcome','description':'Warping was observed with an open enclosure.',
                                    'context':{'enclosure':'open'}}),'actor':'Synthetic operator','recorded_at':now()},mutation=True)
        await accept(feedback['data']['candidate'])
        withdrawal=await call('knowledge.revise',{'record_id':lesson,'entries':[dict(entries[0],state='withdrawn',reason='Trial withdrawn in technical exercise')],
            'actor':'Synthetic reviewer','reason':'Exercise independent surviving support','recorded_at':now()},mutation=True)
        await accept(withdrawal['data']['candidate'])
        after=await call('knowledge.apply',{'record_id':lesson,'context':condition})
        assert after['data']['assessment']['independent_episodes']==['P-22']
        before=await call('knowledge.apply',{'record_id':lesson,'context':condition},publication=semantic_publication)
        assert set(before['data']['assessment']['independent_episodes'])=={'P-14','P-22'}
        await call('history',{'record_id':lesson})
        await call('diff',{'before':semantic_publication,'after':ready})
        pin_publications=[]
        for pin in (child_pins[0],child_pins[1],child_pins[0]):
            proposed=await call('packages.pin',{'path':str(child.base),'package_id':child_id,'commit':pin,
                'mount':'packages/retained','recorded_at':now()},mutation=True)
            await accept(proposed['data']['candidate']);pin_publications.append(ready)
            await call('packages.inspect',{})
        await call('packages.inspect',{},publication=pin_publications[1])
        exported=await call('archive.export',{'path':str(root/'atlas.zip')},mutation=True)
        native['switch_cache'](root/'restored/vectors')
        restore_config=json.loads(config_path.read_text())
        restore_config['service_root']=str(root/'restored-service')
        restore_config_path=root/'restored-service.json'
        restore_config_path.write_text(json.dumps(restore_config))
        restore_params=StdioServerParameters(command='sh',args=[str(package/'scripts/launch.sh')],
            env={**env,'KNOWLEDGE_ATLAS_CONFIG':str(restore_config_path)})
        async with stdio_client(restore_params) as (restore_reader,restore_writer):
          async with ClientSession(restore_reader,restore_writer) as restore_session:
            await restore_session.initialize()
            session=restore_session
            atlas_id=None
            ready=None
            restored=await call('archive.restore',{'archive':str(root/'atlas.zip'),'path':str(root/'restored')},mutation=True)
            restored_id=restored['data']['atlas_id']
            atlas_id=restored_id
            ready=restored['publication']
            restored_read=await call('knowledge.apply',{'record_id':lesson,'context':condition},publication=semantic_publication)
            assert set(restored_read['data']['assessment']['independent_episodes'])=={'P-14','P-22'}
            restored_search=await call('search',{'query':'Fresh restored query '+str(uuid.uuid4()),'mode':'vector','count':1,'eligible':[lesson]})
            assert restored_search['data']['hits'][0]['record']['@id']==lesson
            (root/'qualification.json').write_text(json.dumps({'host':host,'os':platform.platform(),'machine':platform.machine(),
                'profiles':profiles,'runtime':str(native['runtime']),'first_publication':first_publication,
                'semantic_publication':semantic_publication,'ready':ready,'restored_atlas_id':restored_id,
                'input_sha256':[hashlib.sha256(s['content'].encode()).hexdigest() for s in sources]},indent=2))
