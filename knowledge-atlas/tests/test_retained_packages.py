import json
import os
import uuid
from pathlib import Path
import tempfile
import unittest
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.graph_stage import records_for,GraphStage,key
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.native_projection import NativeProjection
from knowledge_atlas.core.reader import AtlasReader
from knowledge_atlas.core.archive import export_atlas,restore_atlas
from knowledge_atlas.core.feedback import revise_compilation

ROOT=Path(__file__).parent/'fixtures'

class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.wiki=json.loads((ROOT/'evidence/wiki-initial-final.json').read_text())
        self.extraction=json.loads((ROOT/'evidence/extractor-output.json').read_text())
        self.parent=GitStore(self.home/'parent');self.child=GitStore(self.home/'child')
        self.base=self.snapshot(self.parent,0);self.first=self.snapshot(self.child,1)

    def snapshot(self,store,version):
        return store.snapshot(self.wiki,self.extraction,{'version':version},recorded_at='2026-11-10T00:00:00Z')['commit']

    def pin(self,base,child_commit,mount):
        return self.parent.pin_package(base,self.child,child_commit,mount=mount,
            recorded_at='2026-11-10T00:01:00Z')

    def test_compilation_revision_preserves_independent_package_pins(self):
        composed=self.pin(self.base,self.first,'shared/child')
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
        nested=self.child.pin_package(self.first,grandchild,third,mount='shared/nested',
            recorded_at='2026-11-10T00:01:00Z')
        grandchild_id=grandchild.read(third)['manifest']['package_id']
        source=next(r for r in records_for(self.child.read(nested),nested)
            if r['kind']=='source' and json.loads(r['payload']).get('package_id')==grandchild_id)
        proposal=propose_records(self.child,nested,[{'id':'nested-quote','kind':'statement',
            'text':'Retained grandchild quotation','provenance':[{'record_id':source['@id'],
            'start':0,'end':len(source['text']),'quote':source['text']}]}],
            actor='Synthetic curator',method='explicit curation',reason='Cite nested source',
            recorded_at='2026-11-10T00:02:00Z')
        parent=self.pin(self.base,proposal,'shared/child')
        projected={r['@id']:r for r in records_for(self.parent.read(parent),parent)}
        quote=next(r for r in projected.values() if r['text']=='Retained grandchild quotation')
        citation=json.loads(quote['payload'])['provenance'][0]
        self.assertEqual(citation['record_id'],source['@id'])
        self.assertIn(source['@id'],quote['links'])
        self.assertEqual(projected[citation['record_id']]['text'],citation['quote'])

    def test_child_updates_do_not_change_pinned_snapshot_and_mounts_preserve_identity(self):
        first=self.pin(self.base,self.first,'shared/first')
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
        second_child=self.snapshot(self.child,2)
        self.assertEqual(self.parent.read(first),snapshot)
        second=self.pin(first,second_child,'shared/first')
        self.assertEqual(self.parent.read(second)['included_packages'][0]['snapshot']['evidence']['version'],2)
        repeated=self.pin(second,second_child,'shared/second')
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
        nested=self.child.pin_package(self.first,grandchild,third,mount='shared/nested',recorded_at='2026-11-10T00:01:00Z')
        parent=self.pin(self.base,nested,'shared/child')
        data=self.parent.read(parent)['included_packages'][0]['snapshot']['included_packages'][0]
        self.assertEqual(data['snapshot']['evidence']['version'],3)
        with self.assertRaisesRegex(ValueError,'cycle'):
            self.child.pin_package(nested,self.parent,parent,mount='shared/back',recorded_at='2026-11-10T00:02:00Z')

