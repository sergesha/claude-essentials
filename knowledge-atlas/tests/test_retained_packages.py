import json
from pathlib import Path
import tempfile
import unittest
from test_packages import layout, publish, selection
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.graph_stage import records_for
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.feedback import revise_compilation

ROOT=Path(__file__).parent/'fixtures'

class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.wiki=json.loads((ROOT/'evidence/wiki-current.json').read_text())
        self.extraction=json.loads((ROOT/'evidence/extraction-current.json').read_text())
        self.parent=GitStore(self.home/'parent');self.child=GitStore(self.home/'child')
        self.base=self.snapshot(self.parent,0)
        self.wrapper=publish(self.child,self.snapshot(self.child,1))
        self.first=Publications(self.child).read(self.wrapper)['content_commit']

    def snapshot(self,store,version):
        return store.snapshot(self.wiki,self.extraction,{'version':version},recorded_at='2026-11-10T00:00:00Z')['commit']

    def test_compilation_revision_preserves_independent_package_pins(self):
        composed=layout(self.parent,selection(self.parent,self.child,self.wrapper))
        before=self.parent.read(composed)
        revised=revise_compilation(self.parent,composed,[],self.wiki,self.extraction,{'version':4},
            actor='Synthetic curator',method='explicit curation',reason='Update root compilation',
            recorded_at='2026-11-10T00:03:00Z')
        after=self.parent.read(revised)
        self.assertEqual(after.get('included_packages'),before['included_packages'])
        self.assertEqual(after['evidence']['version'],4)
        self.assertIn('shared/child',git(self.parent.root,'show',revised+':.gitmodules'))
        self.assertIn('160000 commit '+self.first,git(self.parent.root,'ls-tree',revised,'shared/child'))
        self.assertIsNone(Publications(self.parent).ready())

    def test_nested_cross_package_citation_keeps_its_source_identity(self):
        grandchild=GitStore(self.home/'grandchild');third=self.snapshot(grandchild,3)
        nested=layout(self.child,selection(self.child,grandchild,publish(grandchild,third),'shared/nested'))
        grandchild_id=grandchild.read(third)['manifest']['package_id']
        source=next(r for r in records_for(self.child.read(nested),nested)
            if r['kind']=='source' and json.loads(r['payload']).get('package_id')==grandchild_id)
        proposal=propose_records(self.child,nested,[{'id':'nested-quote','kind':'statement',
            'text':'Retained grandchild quotation','provenance':[{'record_id':source['@id'],
            'start':0,'end':len(source['text']),'quote':source['text']}]}],
            actor='Synthetic curator',method='explicit curation',reason='Cite nested source',
            recorded_at='2026-11-10T00:02:00Z',base_publication=self.wrapper)
        parent=layout(self.parent,selection(self.parent,self.child,publish(self.child,proposal)))
        projected={r['@id']:r for r in records_for(self.parent.read(parent),parent)}
        quote=next(r for r in projected.values() if r['text']=='Retained grandchild quotation')
        citation=json.loads(quote['payload'])['provenance'][0]
        self.assertEqual(citation['record_id'],source['@id'])
        self.assertIn(source['@id'],quote['links'])
        self.assertEqual(projected[citation['record_id']]['text'],citation['quote'])

    def test_child_updates_do_not_change_pinned_snapshot_and_mounts_preserve_identity(self):
        first=layout(self.parent,selection(self.parent,self.child,self.wrapper,'shared/first'))
        snapshot=self.parent.read(first)
        self.assertEqual(len(snapshot['included_packages']),1)
        included=snapshot['included_packages'][0]
        projected=records_for(snapshot,first)
        self.assertEqual(len(projected),2*len(records_for(self.parent.read(self.base),self.base)))
        self.assertEqual(included['snapshot']['evidence']['version'],1)
        self.assertEqual(included['scope'],'all')
        identity=self.child.read(self.first)['manifest']['package_id']
        self.assertEqual(included['package_id'],identity)
        self.assertIn('160000 commit '+self.first,git(self.parent.root,'ls-tree',first,'shared/first'))
        self.assertIn('shared/first',git(self.parent.root,'show',first+':.gitmodules'))
        second_child=revise_compilation(self.child,self.first,[],self.wiki,self.extraction,{'version':2},
            actor='Fixture',method='Synthetic review',reason='Update child',
            recorded_at='2026-11-13T02:00:00Z',base_publication=self.wrapper)
        second_pub=publish(self.child,second_child)
        self.assertEqual(self.parent.read(first),snapshot)
        new_selection=selection(self.parent,self.child,second_pub,'shared/first')
        second=layout(self.parent,new_selection)
        self.assertEqual(self.parent.read(second)['included_packages'][0]['snapshot']['evidence']['version'],2)
        repeated=layout(self.parent,new_selection,{**new_selection,'mount':'shared/second'})
        included=self.parent.read(repeated)['included_packages']
        self.assertEqual(len(included),1)
        self.assertEqual(set(included[0]['mounts']),{'shared/first','shared/second'})
        self.assertEqual(included[0]['package_id'],identity)
        moved=self.home/'relocated';self.child.base.rename(moved)
        (self.parent.base/'package-locations.json').write_text(json.dumps({identity:str(moved/'atlas')}))
        self.assertEqual(self.parent.read(repeated)['included_packages'],included)
        self.assertIsNone(Publications(self.parent).ready())
        self.assertEqual(self.parent.read(first)['included_packages'][0]['snapshot']['evidence']['version'],1)

    def test_nested_package_is_read_and_cyclic_inclusion_is_rejected(self):
        grandchild=GitStore(self.home/'grandchild');third=self.snapshot(grandchild,3)
        nested=layout(self.child,selection(self.child,grandchild,publish(grandchild,third),'shared/nested'))
        parent=layout(self.parent,selection(self.parent,self.child,publish(self.child,nested)))
        data=self.parent.read(parent)['included_packages'][0]['snapshot']['included_packages'][0]
        self.assertEqual(data['snapshot']['evidence']['version'],3)
        with self.assertRaisesRegex(ValueError,'cycle'):
            layout(self.child,selection(self.child,self.parent,publish(self.parent,parent),'shared/back'))


    def test_receipt_free_layout_rejected_without_rewriting_input(self):
        from knowledge_atlas.core.publication import commit_files
        from knowledge_atlas.core.composition import composition_receipts
        current = layout(self.parent, selection(self.parent, self.child, self.wrapper))
        manifest = self.parent.read(current)['manifest']
        mod, = [item for item in manifest['modules'].values() if item.get('kind') == 'atlas']
        del mod['publication']
        historical = commit_files(self.parent.root, current, {'manifest.json':manifest}, [current], 'Content-only inclusion')
        original = git(self.parent.root, 'show', historical + ':manifest.json')
        with self.assertRaises(ValueError):
            composition_receipts(self.parent, historical)
        with self.assertRaises(ValueError):
            self.parent.read(historical)
        self.assertEqual(git(self.parent.root, 'show', historical + ':manifest.json'), original)
