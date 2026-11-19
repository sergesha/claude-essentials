"""Retained PoC9 native type, value, context and provenance assertions."""
import unittest
import os
import pytest
from knowledge_atlas.core.relations import resolve_relations
pytestmark = pytest.mark.skipif(not os.environ.get('POC9_EXTRACT_BIN'), reason='explicit native Utopia binary required')

class NativeExtractTests(unittest.TestCase):
    def raw(self):
        return {'statements':[{'subject':'Claim','phrase':'is supported by','object':'Report','quote':'Claim is supported by Report if dry.','qualifiers':[['condition','dry']], 'when':'2025','ended':'2026','value':None}]}

    def test_native_reverse_binding_preserves_original_context(self):
        result=resolve_relations(self.raw(), model=lambda r:{'b':[[0,'supports','reverse']]})
        row=result['relations'][0]
        self.assertEqual(row['status'],'resolved')
        self.assertEqual((row['subject'],row['object']),('Report','Claim'))
        self.assertEqual(row['ended'],'2026')
        self.assertEqual(row['qualifiers'],[['condition','dry']])

    def test_unknown_and_missing_native_answers_stay_unresolved(self):
        for reply in ({'b':[[0,'invented','forward']]},{'b':[]}):
            result=resolve_relations(self.raw(),model=lambda r:reply)
            self.assertEqual(result['relations'][0]['status'],'unresolved')

    def test_reusable_wiki_api_uses_actual_open_parser(self):
        from knowledge_atlas.core.extract_stage import extract_wiki
        result=extract_wiki({'Archive.md':'Archive needs dry storage.'},model=lambda r:{'e':[], 's':[['Archive needs dry storage.','Archive','needs','dry storage',None,{'condition':'dry'},None,None]],'n':[],'t':[]})
        self.assertEqual(result['statements'][0]['phrase'],'needs')
        self.assertEqual(result['statements'][0]['quote'],'Archive needs dry storage.')

class NativeAdmissionTests(unittest.TestCase):
    def raw(self):
        return {'statements':[{'subject':'Archive','object':'Entry','phrase':'indexes','quote':'Archive indexes Entry.','qualifiers':{'condition':'after review'},'when':None,'ended':None,'value':None}]}

    def model(self, action, reuse='describes'):
        def answer(request):
            if request['operation']=='phrase':
                # Only initial catalog includes same_as; later admission binds
                # against the single admitted/recommended type, through Rust.
                text=request['messages'][1]['content']
                if '- same_as ' in text:return {'b':[[0,None,None]]}
                return {'b':[[0,reuse if action=='reuse' else 'indexes','forward']]}
            if request['operation']=='proposal':return {'p':[{'kind':'property','key':'indexes','label':'indexes','definition':'Archive maintains a lookup index for entry; subject archive, object entry.','value':False,'domains':[],'ranges':[],'s':[0],'k':[],'q':[]}], 'existing':[]}
            return {'decision':action,'uncertain':False,'reason':'Distinct lookup relation' if action=='accept' else 'Requires admission decision','type_id':reuse,'meaning_preserved':True,'why_existing_insufficient':'Maintaining lookup is distinct from describing content','question':'Does lookup imply description?'}
        return answer

    def test_automatic_admission_requires_native_mapping_for_new_participants(self):
        bundle=resolve_relations(self.raw(),model=self.model('accept'))
        self.assertEqual(bundle['relations'][0]['type_id'],'indexes')
        self.assertEqual(bundle['relations'][0]['native_binding']['direction'],'forward')
        self.assertEqual(bundle['catalog'][-1]['origin'],'automatic')
        self.assertEqual(len(bundle['decisions']),1)

    def test_reuse_is_natively_aligned_to_specific_existing_type(self):
        bundle=resolve_relations(self.raw(),model=self.model('reuse'))
        self.assertEqual(bundle['relations'][0]['type_id'],'describes')
        self.assertNotIn('indexes',{t['id'] for t in bundle['catalog']})

    def test_human_admission_does_not_accept_unmapped_relation_and_reject_retains_it(self):
        from knowledge_atlas.core.relations import human_decision
        raw=self.raw();bundle=resolve_relations(raw,model=self.model('human'))
        self.assertEqual(bundle['relations'][0]['status'],'needs_human')
        decision=bundle['decisions'][0]['id']
        accepted=human_decision(bundle,raw,decision,action='accept',actor='Curator',reason='Distinct lookup relation')
        self.assertEqual(accepted['relations'][0]['status'],'unresolved')
        rejected=human_decision(bundle,raw,decision,action='reject',actor='Curator',reason='Unneeded')
        self.assertEqual(rejected['relations'][0]['status'],'rejected')
        self.assertEqual(rejected['relations'][0]['quote'],'Archive indexes Entry.')

class NativeContextTests(unittest.TestCase):
    def test_native_null_keeps_negated_conditional_statement_and_both_times(self):
        raw={'statements':[{'subject':'Archive','object':'Release','phrase':'does not permit',
            'quote':'Archive does not permit Release unless reviewed in 2025–2026.',
            'qualifiers':{'condition':'unless reviewed'},'when':'2025','ended':'2026','value':None}]}
        def reply(request):
            return {'b':[[0,None,None]]} if request['operation']=='phrase' else {'p':[],'existing':[]}
        result=resolve_relations(raw,model=reply)
        row=result['relations'][0]
        self.assertEqual(row['status'],'unresolved')
        for field in ('phrase','quote','qualifiers','when','ended'):
            self.assertEqual(row[field],raw['statements'][0][field])
        self.assertNotIn('polarity',row)

    def test_actual_proposal_parser_rejects_foreign_signature_ids(self):
        from knowledge_atlas.core.extract_stage import native
        parsed=native('proposal_parse',{'items':[{'id':0,'phrase':'indexes','quotes':[],'examples':[]}],'catalog':[],
            'response':'{"p":[{"kind":"property","key":"indexes","label":"indexes","definition":"Archive indexes entry","value":false,"domains":[],"ranges":[],"s":[99],"k":[],"q":[]}],"existing":[]}'})
        self.assertEqual(parsed['proposals'],[])
        self.assertGreater(parsed['malformed'],0)

class GroupedAdmissionTests(unittest.TestCase):
    def run_group(self, action):
        import copy
        raw=NativeAdmissionTests().raw()
        raw['statements'].append({**copy.deepcopy(raw['statements'][0]),'subject':'Library','quote':'Library indexes Entry.'})
        calls=[];base=NativeAdmissionTests().model(action)
        def model(request):
            calls.append(request)
            answer=base(request)
            if request['operation']=='proposal':
                answer['p'][0]['s']=[0,1]
            return answer
        return raw,resolve_relations(raw,model=model),calls

    def test_one_type_review_keeps_all_contexts_and_individual_bindings(self):
        for action in ('accept','reuse','human','reject'):
            with self.subTest(action=action):
                raw,bundle,calls=self.run_group(action)
                reviews=[r for r in calls if r['operation']=='review_extension']
                self.assertEqual(len(reviews),1)
                self.assertEqual(reviews[0]['statements'],[{'index':i,'statement':s} for i,s in enumerate(raw['statements'])])
                self.assertEqual(len(bundle['decisions']),1)
                self.assertEqual(bundle['decisions'][0]['indices'],[0,1])
                self.assertEqual(len({r['decision_id'] for r in bundle['relations']}),1)
                expected={'accept':['resolved','unresolved'],'reuse':['resolved','unresolved'],
                          'human':['needs_human','needs_human'],'reject':['rejected','rejected']}[action]
                self.assertEqual([r['status'] for r in bundle['relations']],expected)
                self.assertEqual(bundle['relations'][1]['quote'],'Library indexes Entry.')

    def test_human_type_decision_applies_to_group_without_inventing_bindings(self):
        from knowledge_atlas.core.relations import human_decision
        raw,bundle,_=self.run_group('human')
        decision=bundle['decisions'][0]['id']
        for action in ('accept','reuse','reject'):
            with self.subTest(action=action):
                result=human_decision(bundle,raw,decision,action=action,actor='Curator',reason='Type decision',type_id='describes')
                self.assertEqual([r['status'] for r in result['relations']],['rejected']*2 if action=='reject' else ['unresolved']*2)
        result=human_decision(bundle,raw,decision,action='accept',actor='Curator',reason='Distinct type',model=NativeAdmissionTests().model('accept'))
        self.assertEqual([r['status'] for r in result['relations']],['resolved','unresolved'])

    def test_different_native_definitions_are_not_merged(self):
        import copy
        raw=NativeAdmissionTests().raw()
        raw['statements'].append({**copy.deepcopy(raw['statements'][0]),'quote':'A different context.'})
        calls=[];base=NativeAdmissionTests().model('human')
        def model(request):
            calls.append(request)
            answer=base(request)
            if request['operation']=='proposal':
                other=copy.deepcopy(answer['p'][0]);other['s']=[1]
                other['key']='cross_indexes'
                other['definition']='Creates a subject index for an entry, distinct proposed definition.'
                answer['p'].append(other)
            return answer
        result=resolve_relations(raw,model=model)
        self.assertEqual(len([r for r in calls if r['operation']=='review_extension']),2)
        self.assertEqual([d['indices'] for d in result['decisions']],[[0],[1]])

class NativeAttributeTests(unittest.TestCase):
    def test_literal_binding_retains_value_and_native_kind(self):
        from knowledge_atlas.core.relations import core_catalog
        self.assertEqual(next(t for t in core_catalog() if t['id']=='has_property')['kind'],'attribute')
        for value in ('dry',0):
            raw={'statements':[{'subject':'Sample','object':None,'value':value,'phrase':'moisture',
                'quote':'Sample moisture measured.','qualifiers':[['unit','percent']],'when':'2026','ended':None}]}
            calls=[]
            def model(request):
                calls.append(request)
                return {'b':[[0,'has_property','forward']]}
            bundle=resolve_relations(raw,model=model)
            row=bundle['relations'][0]
            self.assertEqual(row['status'],'resolved')
            self.assertIsNone(row['object'])
            self.assertEqual(row['value'],value)
            self.assertEqual(row['qualifiers'],raw['statements'][0]['qualifiers'])
            self.assertIn('· attribute ·',calls[0]['messages'][1]['content'])

    def test_incompatible_native_shape_is_not_materialized(self):
        for key,direction,obj,value in [('has_property','reverse',None,'dry'),('supports','forward',None,'dry'),('has_property','forward','State',None)]:
            raw={'statements':[{'subject':'Sample','object':obj,'value':value,'phrase':'condition','quote':'Sample condition.'}]}
            def model(request):
                return {'b':[[0,key,direction]]} if request['operation']=='phrase' else {'p':[],'existing':[]}
            self.assertEqual(resolve_relations(raw,model=model)['relations'][0]['status'],'unresolved')

    def test_human_attribute_binding_preserves_zero_value(self):
        from knowledge_atlas.core.relations import human_decision
        raw={'statements':[{'subject':'Sample','object':None,'value':0,'phrase':'moisture',
            'quote':'Sample moisture is zero.','qualifiers':[['unit','percent']]}]}
        def model(request):
            if request['operation']=='phrase':return {'b':[[0,None,None]]}
            if request['operation']=='proposal':return {'p':[{'kind':'property','key':'moisture',
                'label':'moisture','definition':'Moisture content of sample','value':True,
                'domains':[],'ranges':[],'s':[0],'k':[],'q':[]}],'existing':[]}
            return {'decision':'human','uncertain':True,'reason':'Definition needs a decision','question':'Separate moisture type?'}
        bundle=resolve_relations(raw,model=model)
        result=human_decision(bundle,raw,bundle['decisions'][0]['id'],action='reuse',
            actor='Curator',reason='Use attribute type',type_id='has_property',
            model=lambda r:{'b':[[0,'has_property','forward']]})
        row=result['relations'][0]
        self.assertEqual(row['status'],'resolved')
        self.assertIsNone(row['object'])
        self.assertEqual(row['value'],0)

class WikiOriginTests(unittest.TestCase):
    def test_exact_page_origin_survives_shared_or_missing_quote(self):
        from knowledge_atlas.core.extract_stage import extract_wiki
        from knowledge_atlas.core.git_store import GitStore
        from knowledge_atlas.core.graph_stage import records_for
        from pathlib import Path
        import tempfile,json
        pages={'one/A.md':'A supports B.','two/B.md':'A supports B.'}
        for quote in ('A supports B.',None):
            raw=extract_wiki(pages,model=lambda r:{'e':[['A','thing',1],['B','thing',1]],
                's':[[quote,'A','supports','B',None,None,None,None]],'n':[],'t':[]})
            self.assertEqual([s['wiki_page'] for s in raw['statements']],list(pages))
            self.assertNotIn('wiki_page',raw['model_calls'][0]['parsed']['statements'][0])
            wiki={'sources':[],'references':[],'pages':[{'id':i+1,'path':k.rsplit('/',1)[0]+'/',
                'filename':k.rsplit('/',1)[1],'content':v} for i,(k,v) in enumerate(pages.items())]}
            with tempfile.TemporaryDirectory() as tmp:
                store=GitStore(Path(tmp)/'store')
                oid=store.snapshot(wiki,raw,{},recorded_at='2026-11-10T00:00:00Z')['commit']
                snapshot=store.read(oid);docs=records_for(snapshot,oid)
                wiki_ids={json.loads(d['payload'])['path']+json.loads(d['payload'])['filename']:d['@id'] for d in docs if d['kind']=='wiki'}
                for d in docs:
                    if d['kind']=='statement':
                        row=json.loads(d['payload'])
                        self.assertEqual(set(d['links']) & set(wiki_ids.values()),{wiki_ids[row['wiki_page']]})
                # Explicitly inconsistent origin must not silently gain a guessed link.
                snapshot.pop('relations')
                snapshot['extraction']['statements'][0]['wiki_page']='missing.md'
                with self.assertRaisesRegex(ValueError,'Wiki page'):records_for(snapshot,oid)

class NativeMoodTests(unittest.TestCase):
    def test_explicit_mood_is_retained_without_type_proposal(self):
        import copy
        raw=NativeExtractTests().raw()
        raw['statements'][0]['qualifiers']=[['mood','should'],['condition','dry']]
        calls=[]
        def model(request):
            calls.append(request);return {'b':[[0,'supports','reverse']]}
        bundle=resolve_relations(raw,model=model)
        self.assertEqual(calls,[])
        self.assertEqual(bundle['relations'][0]['status'],'unresolved')
        self.assertIn('mood',bundle['relations'][0]['reason'])
        for field in ('quote','phrase','qualifiers','when','ended'):
            self.assertEqual(bundle['relations'][0][field],raw['statements'][0][field])
        plain=copy.deepcopy(raw);plain['statements'][0]['qualifiers']=[['condition','dry']]
        earlier=resolve_relations(plain,model=model)
        self.assertEqual(earlier['relations'][0]['status'],'resolved')
        calls.clear()
        current=resolve_relations(raw,model=model,previous=earlier)
        self.assertEqual(current['relations'][0]['status'],'unresolved')
        self.assertEqual(calls,[])

    def test_mood_does_not_block_other_assertions_or_become_a_resolved_bundle(self):
        import copy
        from knowledge_atlas.core.relations import validate_bundle
        raw=NativeExtractTests().raw()
        raw['statements'].append(copy.deepcopy(raw['statements'][0]))
        raw['statements'][0]['qualifiers']=[['mood','if approved']]
        calls=[]
        def model(request):
            calls.append(request)
            self.assertNotIn('Item 0:',request['messages'][1]['content'])
            return {'b':[[1,'supports','reverse']]}
        bundle=resolve_relations(raw,model=model)
        self.assertEqual(len(calls),1)
        self.assertEqual([r['status'] for r in bundle['relations']],['unresolved','resolved'])
        invalid=copy.deepcopy(bundle)
        invalid['relations'][0].update(status='resolved',type_id='supports',polarity='positive',details={})
        with self.assertRaises(ValueError):validate_bundle(invalid,raw)
