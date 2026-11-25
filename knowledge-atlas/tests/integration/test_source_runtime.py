"""Native Wiki removal contract; deterministic MCP client, no LLM or acceptance."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest


def register_native_project(root, store):
    from knowledge_atlas.contracts import ServiceContext
    from knowledge_atlas.project_registry import register_project, repository_identity
    return register_project(ServiceContext(root/'service'), repository_identity(store.base), store.base).atlas_id


@unittest.skipUnless(os.environ.get('KNOWLEDGE_ATLAS_WIKI_ROOT'), 'explicit native Wiki runtime required')
class NativeSourceTests(unittest.TestCase):
    def test_removal_reconciles_native_page_without_changing_history(self):
        from knowledge_atlas.core.wiki_stage import WikiStage
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def check():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                first_source, second_source = root/'first.txt', root/'second.txt'
                first_source.write_text('The first trial used wet cleaning.')
                second_source.write_text('The second trial used dry cleaning.')
                initial = 'Wet cleaning in first trial.[^1]\nDry cleaning in second trial.[^2]\n\n[^1]: first.txt\n[^2]: second.txt'
                corrected = 'Dry cleaning in second trial.[^2]\n\n[^2]: second.txt'
                first = await WikiStage(root/'initial').run(
                    [{'path':str(first_source)}, {'path':str(second_source)}],
                    [{'path':'/wiki/','title':'Trials','content':initial,'tags':['trial']}],
                    recorded_at='2026-11-13T00:00:00Z')
                first['sources'][0].update(source_id='one', name='note.txt')
                first['sources'][1].update(source_id='two', name='note.txt')
                async def agent(request):
                    self.assertEqual(request['removed_sources'], ['first.txt'])
                    async with stdio_client(StdioServerParameters(**request['mcp'])) as (read,write):
                        async with ClientSession(read,write) as session:
                            await session.initialize()
                            result = await session.call_tool('edit', {
                                'knowledge_base':request['knowledge_base'],'path':'/wiki/trials.md',
                                'old_string':initial,'new_string':corrected})
                            self.assertFalse(result.isError, result)
                            self.assertNotIn('Error:', str(result))
                            metadata = await session.call_tool('edit', {
                                'knowledge_base':request['knowledge_base'],'path':'/wiki/trials.md',
                                'old_string':'description: Wet cleaning in first trial.[^1]',
                                'new_string':'description: Dry cleaning in second trial.[^2]'})
                            self.assertFalse(metadata.isError, metadata)
                            self.assertNotIn('Error:', str(metadata))
                    return {'status':'completed'}
                second = await WikiStage(root/'update').run_agent([], agent, previous=first,
                    remove_sources=['first.txt'], recorded_at='2026-11-13T01:00:00Z')
                self.assertEqual([s['source_id'] for s in second['sources']], ['two'])
                self.assertNotIn('Wet cleaning', second['pages'][0]['content'])
                self.assertIn('Dry cleaning', second['pages'][0]['content'])
                self.assertIn('Wet cleaning', first['pages'][0]['content'])
                self.assertEqual(second['sources'][0]['id'], first['sources'][1]['id'])
                self.assertEqual(second['sources'][0]['recorded_at'], first['recorded_at'])
                self.assertFalse((root/'update/first.txt').exists())
                valid_ids = {d['id'] for d in second['pages'] + second['sources']}
                self.assertTrue(all(edge['source_id'] in valid_ids and edge['target_id'] in valid_ids for edge in second['references']))
        asyncio.run(check())

    def test_packaged_pipeline_reuses_native_wiki_and_extractor(self):
        import json
        from unittest.mock import patch
        from knowledge_atlas.contracts import ActionContext, ActionRequest, ServiceContext
        from knowledge_atlas.actions.atlas import create_atlas
        from knowledge_atlas.native_pipeline import execute_native
        from knowledge_atlas.core.git_store import GitStore
        from knowledge_atlas.core.publication import Publications
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        class DeterministicModel:
            def __init__(self, **_): pass
            def effective_profile(self, stage): return {'model':'deterministic-native-boundary-fixture','stage':stage}
            def __call__(self, request):
                if request['operation'] != 'open': raise AssertionError('unexpected alignment for empty extraction')
                return {'e':[], 's':[], 'n':[], 't':[]}
            async def wiki(self, request):
                text = '\n'.join(Path(request['workspace'], source['filename']).read_text() + f"[^{i}]\n\n[^{i}]: {source['filename']}"
                    for i,source in enumerate(request['sources'],1))
                async with stdio_client(StdioServerParameters(**request['mcp'])) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        result = await session.call_tool('create', {'knowledge_base':request['knowledge_base'],
                            'path':'/wiki/', 'title':'Observations', 'content':text, 'tags':['observation']})
                        if result.isError or 'Error:' in str(result): raise AssertionError(result)
                return {'status':'completed'}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); evidence = root/'attempt'; evidence.mkdir()
            service = ServiceContext(root/'service')
            created = create_atlas(ActionContext(service, ActionRequest('atlas.create'), root/'atlas', evidence, {}))
            payload = {'request':{'action':'sources.submit','atlas_id':created.data['atlas_id'],'payload':{
                'sources':[{'source_id':'one','name':'note.md','content':'First observation.'},
                           {'source_id':'two','name':'note.md','content':'Second observation.'}],
                'recorded_at':'2026-11-13T00:00:00Z'}}, 'configuration':{},
                'service_root':str(root/'service'), 'vector_cache':str(root/'service/vectors'), 'atlas_path':str(root/'atlas'),'evidence':str(evidence)}
            with patch('knowledge_atlas.core.model_config.ConfiguredModels', DeterministicModel):
                result = execute_native(payload)
            store = GitStore(root/'atlas')
            saved = store.read(result.data['candidate'])
            self.assertEqual(result.data['status'],'pending_review')
            self.assertEqual({row['source_id'] for row in saved['wiki']['sources']}, {'one','two'})
            self.assertEqual(len(saved['sources']),2)
            self.assertIsNone(saved['manifest']['proposal_base_publication'])
            self.assertTrue(all(row['recorded_at'] == saved['wiki']['recorded_at'] for row in saved['wiki']['sources']))
            self.assertEqual(saved['manifest']['status'],'candidate')
            self.assertIsNone(Publications(store).ready())
            self.assertEqual(len(saved['extraction']['model_calls']),1)
            self.assertTrue((evidence/'native/execution.json').is_file())

    def test_native_revision_preserves_candidate_and_passes_correction(self):
        import json
        import uuid
        from dataclasses import asdict
        from unittest.mock import patch
        from knowledge_atlas.contracts import ActionRequest
        from knowledge_atlas.native_pipeline import execute_native
        from knowledge_atlas.core.git_store import GitStore
        from knowledge_atlas.core.publication import Publications
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        test = self
        class CorrectionModel:
            def __init__(self, **_): pass
            def effective_profile(self, stage): return {'model':'native-revision-fixture','stage':stage}
            def __call__(self, request): return {'e':[], 's':[], 'n':[], 't':[]}
            async def wiki(self, request):
                test.assertEqual(request['correction_instructions'], 'Append the explicit correction note.')
                async with stdio_client(StdioServerParameters(**request['mcp'])) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        page = previous['wiki']['pages'][0]
                        path = page['path'] + page['filename'] if page['path'].endswith('/') else page['path']
                        result = await session.call_tool('append', {'knowledge_base':request['knowledge_base'],
                            'path':path, 'content':'\nExplicit correction note: this remains unreviewed.\n'})
                        test.assertFalse(result.isError, result)
                        test.assertNotIn('Error:', str(result))
                return {'status':'completed'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); evidence = root/'attempt'; evidence.mkdir()
            store = GitStore(root/'atlas')
            wiki = json.loads((Path(__file__).resolve().parents[1]/'fixtures/evidence/wiki-initial-final.json').read_text())
            original = store.snapshot(wiki, {'entities':[], 'statements':[]}, {}, recorded_at='2026-11-13T00:00:00Z')['commit']
            previous = store.read(original)
            request = ActionRequest('candidate.revise', atlas_id=register_native_project(root, store), request_id=str(uuid.uuid4()), payload={
                'candidate':original, 'actor':'Fixture', 'kind':'synthetic', 'reason':'Correction',
                'decided_at':'2026-11-13T01:00:00Z', 'instructions':'Append the explicit correction note.'})
            with patch('knowledge_atlas.core.model_config.ConfiguredModels', CorrectionModel):
                result = execute_native({'request':asdict(request), 'configuration':{'attribution_channel':'python'},
                    'service_root':str(root/'service'), 'vector_cache':str(root/'service/vectors'), 'atlas_path':str(store.base),'evidence':str(evidence)})
            revised = store.read(result.data['candidate'])
            self.assertNotEqual(result.data['candidate'], original)
            self.assertEqual(store.read(original), previous)
            self.assertIn('Explicit correction note', revised['wiki']['pages'][0]['content'])
            self.assertIsNone(Publications(store).ready())

    def test_native_process_failure_retains_evidence_without_host_calls(self):
        from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError, RuntimeConfig, ServiceContext
        from knowledge_atlas.actions.sources import submit_sources
        from knowledge_atlas.actions.atlas import create_atlas
        from knowledge_atlas.core.git_store import git
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runtime = RuntimeConfig(Path(os.environ['KNOWLEDGE_ATLAS_WIKI_ROOT']).parent)
            service = ServiceContext(root/'service', runtime=runtime)
            created = create_atlas(ActionContext(service, ActionRequest('atlas.create'), root/'atlas', root/'attempt', {}))
            refs = git(root/'atlas/atlas', 'show-ref')
            context = ActionContext(service, ActionRequest('sources.submit',atlas_id=created.data['atlas_id'],
                                                         payload={'recorded_at':'invalid'}),
                                    root/'atlas', root/'attempt', {})
            with self.assertRaises(AtlasError) as error:
                submit_sources(context)
            self.assertEqual(error.exception.code,'invalid_request')
            self.assertTrue((root/'attempt/native-stderr.txt').is_file())
            self.assertTrue((root/'attempt/native-result.json').is_file())
            self.assertEqual(git(root/'atlas/atlas', 'show-ref'), refs)

    def test_type_handler_realigns_without_accepting_knowledge(self):
        import json
        from unittest.mock import patch
        from knowledge_atlas.native_pipeline import execute_native
        from knowledge_atlas.core.git_store import GitStore
        from knowledge_atlas.core.publication import Publications
        from knowledge_atlas.core.relations import resolve_relations
        raw = {'statements':[{'subject':'Archive','object':'Entry','value':None,'phrase':'indexes',
                             'quote':'Archive indexes Entry.','qualifiers':[],'when':None,'ended':None}]}
        def pending_model(request):
            if request['operation'] == 'phrase': return {'b':[[0,None,None]]}
            if request['operation'] == 'proposal': return {'p':[{'kind':'property','key':'indexes','label':'indexes',
                'definition':'Archive maintains a lookup index for entry; subject archive, object entry.',
                'value':False,'domains':[],'ranges':[],'s':[0],'k':[],'q':[]}], 'existing':[]}
            return {'decision':'human','uncertain':True,'reason':'Needs explicit type decision','question':'Is lookup distinct?'}
        class AlignmentModel:
            def __init__(self, **_): pass
            def __call__(self, request):
                if request['operation'] != 'phrase': raise AssertionError(request['operation'])
                return {'b':[[0,'indexes','forward']]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); evidence=root/'attempt'; evidence.mkdir()
            store = GitStore(root/'atlas')
            wiki = json.loads((Path(__file__).resolve().parents[1]/'fixtures/evidence/wiki-initial-final.json').read_text())
            pending = resolve_relations(raw, pending_model)
            before = store.snapshot(wiki,raw,{},recorded_at='2026-11-13T00:00:00Z',relation_bundle=pending)['commit']
            payload = {'request':{'action':'type.decide','atlas_id':register_native_project(root, store),'payload':{'candidate':before,
                'decision_id':pending['decisions'][0]['id'],'action':'accept','actor':'Synthetic test fixture',
                'reason':'Exercise explicit type decision without knowledge acceptance','recorded_at':'2026-11-13T01:00:00Z'}},
                'configuration':{},'service_root':str(root/'service'), 'vector_cache':str(root/'service/vectors'), 'atlas_path':str(store.base),'evidence':str(evidence)}
            with patch('knowledge_atlas.core.model_config.ConfiguredModels', AlignmentModel):
                result = execute_native(payload)
            after = store.read(result.data['candidate'])
            self.assertEqual(after['relations']['relations'][0]['status'],'resolved')
            self.assertEqual(after['manifest']['status'],'candidate')
            self.assertEqual(store.read(before)['relations']['relations'][0]['status'],'needs_human')
            self.assertIsNone(Publications(store).ready())
