import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class LauncherTests(unittest.TestCase):
    def run_launcher(self, mode='shared', fail_pull=False, cached=False, lookup_fails=False, ref=None, shell="bash"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin = root / 'bin'; bin.mkdir()
            work = root / 'cache/redis-memory-mcp'; work.mkdir(parents=True)
            (work / '.installed-ref').write_text('redis-memory-mcp-v0.12.0\n')
            (work / 'docker-compose.yaml').write_text((ROOT / 'docker-compose.yaml').read_text())
            (work / 'docker-compose.local.yaml').write_text('services: {}\n')
            log = root / 'calls'
            stub = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps([Path(sys.argv[0]).name,*a])+'\\n')
if Path(sys.argv[0]).name=='curl':
 if 'https://api.github.com/repos/sergesha/claude-essentials/releases' in a:
  if os.environ['LOOKUP_FAIL']=='1':sys.exit(22)
  print('[{"tag_name": "redis-memory-mcp-v0.12.0"}]')
 elif '-o' in a:Path(a[a.index('-o')+1]).write_text('services: {}\\n')
else:
 if a[:2]==['image','inspect']:
  if '--format' in a:print('ghcr.io/sergesha/redis-memory-mcp@sha256:'+'a'*64)
  elif os.environ['CACHED']!='1':sys.exit(1)
 if a[0]=='pull' and os.environ['FAIL_PULL']=='1':sys.exit(1)
'''
            for command in ['curl', 'docker']:
                p=bin/command;p.write_text(stub);p.chmod(0o755)
            env={**os.environ,'PATH':str(bin)+os.pathsep+os.environ['PATH'], 'XDG_CACHE_HOME':str(root/'cache'),
                 'CALLS':str(log),'LOOKUP_FAIL':str(int(lookup_fails)),'CACHED':str(int(cached)),
                 'FAIL_PULL':str(int(fail_pull)),'REDIS_MEMORY_MCP_MODE':mode,
                 'REDIS_URL':'redis://example:6379/0','EMBED_URL':'http://example:8082',
                 'NAMESPACE':'same-namespace','CHUNK_MAX_TOKENS':'256','EMBED_DIMENSION':'256'}
            env.pop('REDIS_MEMORY_MCP_REF',None)
            if ref is not None:env['REDIS_MEMORY_MCP_REF']=ref
            result=subprocess.run([shell,str(ROOT/'start.sh')],env=env,capture_output=True,text=True,timeout=10)
            calls=[json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            return result,calls

    def test_system_bash_handles_empty_optional_arrays(self):
        result,_=self.run_launcher(shell='/bin/bash')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_shared_pulls_and_runs_without_build_or_infrastructure(self):
        result,calls=self.run_launcher()
        self.assertEqual(result.returncode,0,result.stderr)
        docker=[c[1:] for c in calls if c[0]=='docker']
        self.assertIn(['pull','ghcr.io/sergesha/redis-memory-mcp:0.12.0'],docker)
        self.assertFalse(any(c[0] in ['build','compose'] for c in docker))
        run=next(c for c in docker if c[0]=='run')
        self.assertEqual(run[-1],'ghcr.io/sergesha/redis-memory-mcp@sha256:'+'a'*64)
        self.assertIn('NAMESPACE',run)
        self.assertNotIn('same-namespace',run)

    def test_dedicated_keeps_override_and_never_builds(self):
        result,calls=self.run_launcher(mode='dedicated')
        self.assertEqual(result.returncode,0,result.stderr)
        compose=[c for c in calls if c[:2]==['docker','compose']]
        self.assertTrue(compose)
        self.assertTrue(all(any(x.endswith('/docker-compose.local.yaml') for x in c) for c in compose))
        self.assertTrue(any('--no-build' in c for c in compose))
        self.assertFalse(any('--build' in c for c in calls))

    def test_pull_failure_aborts_without_source_fallback(self):
        result,calls=self.run_launcher(fail_pull=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(c[:2]==['docker','run'] for c in calls))
        self.assertFalse(any(c[:2]==['docker','build'] for c in calls))

    def test_offline_cached_release_runs(self):
        result,calls=self.run_launcher(cached=True,lookup_fails=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(any(c[:2]==['docker','pull'] for c in calls))

    def test_development_ref_requires_explicit_development_build(self):
        result,calls=self.run_launcher(ref='main')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('docker-compose.build.yaml',result.stderr)
        self.assertFalse(any(c[0]=='docker' for c in calls))

if __name__=='__main__':unittest.main()
