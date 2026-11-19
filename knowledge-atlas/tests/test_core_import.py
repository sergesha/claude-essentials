"""The reusable core must import without a prepared host or native runtime."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class CoreImportTests(unittest.TestCase):
    def test_core_import_has_no_host_or_service_side_effects(self):
        script = '''
import importlib, pkgutil, socket, subprocess, sys

def forbidden(*args, **kwargs):
    raise AssertionError("import attempted a subprocess or network connection")
subprocess.Popen = forbidden
socket.create_connection = forbidden
import knowledge_atlas.core as core
before = list(sys.path)
for module in pkgutil.iter_modules(core.__path__):
    importlib.import_module(core.__name__ + "." + module.name)
assert sys.path == before, "core import changed native module search paths"
'''
        env = {**os.environ, 'PATH': '', 'PYTHONPATH': str(ROOT / 'src')}
        for key in list(env):
            if key.startswith(('POC9_', 'KNOWLEDGE_ATLAS_')):
                env.pop(key)
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

class RelocationTests(unittest.TestCase):
    def test_relocated_core_reads_retained_package(self):
        import json
        import shutil
        import tempfile
        from knowledge_atlas.core.git_store import GitStore
        fixtures = Path(__file__).parent / 'fixtures'
        wiki = json.loads((fixtures / 'evidence/wiki-initial-final.json').read_text())
        extraction = json.loads((fixtures / 'evidence/extractor-output.json').read_text())
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / 'original'
            store = GitStore(original)
            candidate = store.snapshot(wiki, extraction, {}, recorded_at='2026-11-09T21:00:00Z')
            destination = Path(temporary) / 'relocated package with spaces'
            shutil.move(original, destination)
            restored = GitStore(destination)
            saved = restored.read(candidate['commit'])
            self.assertEqual(saved['manifest']['package_id'], candidate['package_id'])
            self.assertEqual(restored.closure(candidate['commit'])[0]['commit'], candidate['commit'])
            self.assertEqual(saved['wiki'], wiki)
            self.assertEqual(saved['sources']['review.txt'], (fixtures / 'fixtures/printlab/review.txt').read_bytes())

if __name__ == '__main__':
    unittest.main()
