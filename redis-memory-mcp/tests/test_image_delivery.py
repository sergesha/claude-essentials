import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('publisher',ROOT/'scripts/publish_images.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)

class ImageDeliveryTests(unittest.TestCase):
    def test_registry_not_found_is_the_only_absence(self):
        ref='ghcr.io/sergesha/redis-memory-mcp:0.12.0'
        for error,absent in [(f'ERROR: {ref}: not found',True),('unauthorized',False),('timeout',False),('too many requests',False)]:
            with self.subTest(error=error),patch.object(publisher.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'',error.encode())):
                if absent:self.assertIsNone(publisher.manifest(ref))
                else:
                    with self.assertRaises(RuntimeError):publisher.manifest(ref)
    def test_immutable_platform_identity(self):
        publisher.check_single({'config':{'digest':'sha256:a'}},'sha256:a')
        with self.assertRaises(ValueError):publisher.check_single({'config':{'digest':'sha256:b'}},'sha256:a')
    def test_exact_two_architecture_index(self):
        expected={'amd64':'sha256:a','arm64':'sha256:b'}
        entries=[{'platform':{'os':'linux','architecture':a},'digest':d} for a,d in expected.items()]
        publisher.check_index({'manifests':entries},expected)
        for wrong in [entries[:1],entries+[entries[0]],[dict(entries[0],digest='sha256:wrong'),entries[1]]]:
            with self.assertRaises(ValueError):publisher.check_index({'manifests':wrong},expected)
    def test_last_candidate_gate_failure_prevents_every_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for component in publisher.COMPONENTS:
                for arch in publisher.ARCHES:
                    reports=root/f'redis-candidate-{component}-{arch}'/'reports'
                    reports.mkdir(parents=True)
                    (reports/'candidate.json').write_text(json.dumps({'version':'0.12.0','revision':'a'*40,
                        'platform':f'linux/{arch}','id':'sha256:'+'b'*64}))
            calls=[]
            def execute(*args):
                calls.append(args)
                if 'verify' in args and 'embeddings-arm64' in args[-1]:
                    raise subprocess.CalledProcessError(2,args)
                return b''
            with patch.object(publisher,'run',side_effect=execute),patch.object(publisher,'manifest') as registry:
                with self.assertRaises(subprocess.CalledProcessError):
                    publisher.publish(root,'0.12.0','a'*40,root/'output.json')
                registry.assert_not_called()
            self.assertFalse(any('push' in call for call in calls))

    def test_publisher_waits_for_all_candidates(self):
        # Parse workflow structure when PyYAML is available in the CI test environment.
        import yaml
        workflow=yaml.safe_load((ROOT.parent/'.github/workflows/release-please.yml').read_text())
        self.assertEqual(workflow['jobs']['redis-publish']['needs'],['release-please','redis-images'])
        self.assertNotIn('if',workflow['jobs']['redis-publish'])
        candidates=yaml.safe_load((ROOT.parent/'.github/workflows/redis-memory-images.yml').read_text())
        job=candidates['jobs']['candidates']
        self.assertEqual(job['strategy']['matrix']['target'],['mcp','embeddings'])
        self.assertEqual(job['strategy']['matrix']['arch'],['amd64','arm64'])
        scan=next(s for s in job['steps'] if s.get('uses')=='./.github/actions/plugin-image-security')
        self.assertNotIn('continue-on-error',scan)
        self.assertNotIn('if',scan)

if __name__=='__main__':unittest.main()
