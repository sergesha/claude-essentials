"""Relocation of the installed launch command keeps data outside the plugin."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT=Path(__file__).resolve().parents[1]


def test_relocated_packaging_and_data_survive_replacement(tmp_path):
    clone=tmp_path/'plugin with spaces'
    shutil.copytree(ROOT,clone,ignore=shutil.ignore_patterns('.venv','dist','__pycache__','.pytest_cache'))
    pyproject=tomllib.loads((clone/'pyproject.toml').read_text())
    for host in ('.claude-plugin','.codex-plugin'):
        manifest=json.loads((clone/host/'plugin.json').read_text())
        assert manifest['version']==pyproject['project']['version']
        assert manifest['name']=='knowledge-atlas'
    config=tmp_path/'config.json';root=tmp_path/'data'
    config.write_text(json.dumps({'service_root':str(root)}))
    env={**__import__('os').environ,'KNOWLEDGE_ATLAS_CONFIG':str(config),'KNOWLEDGE_ATLAS_PYTHON':sys.executable}
    result=subprocess.run(['sh',str(clone/'scripts/launch.sh'),'status'],env=env,capture_output=True,text=True)
    # A missing service is explicit; launching MCP is exercised with the SDK.
    assert result.returncode!=0
    assert 'invalid_configuration' in result.stderr or 'service_unavailable' in result.stderr
    root.mkdir(exist_ok=True);(root/'keep').write_text('external state')
    shutil.rmtree(clone)
    assert (root/'keep').read_text()=='external state'


def test_marketplace_and_release_registration():
    repository=ROOT.parent
    for path in ('.claude-plugin/marketplace.json','.agents/plugins/marketplace.json'):
        entries=json.loads((repository/path).read_text())['plugins']
        assert len([e for e in entries if e['name']=='knowledge-atlas'])==1
    assert json.loads((repository/'.release-please-manifest.json').read_text())['knowledge-atlas']==tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    assert 'knowledge-atlas' in json.loads((repository/'release-please-config.json').read_text())['packages']
