"""Provider routing preserves PoC9 stage guidance and independent reuse identity."""
import copy
import pytest
from knowledge_atlas.providers.config import resolve_profile, default_profiles
from knowledge_atlas.providers.base import ProviderError


def test_stage_override_changes_only_selected_profile():
    original = default_profiles()
    changed = copy.deepcopy(original)
    changed['open']['effort'] = 'low'
    assert resolve_profile(original, 'open') != resolve_profile(changed, 'open')
    for stage in ('wiki', 'phrase', 'proposal', 'review_extension', 'grounded_answer'):
        assert resolve_profile(original, stage) == resolve_profile(changed, stage)


def test_changed_guidance_invalidates_reuse():
    original = default_profiles()
    changed = copy.deepcopy(original)
    changed['phrase']['guidance'] += '\nAdditional explicit guidance.'
    assert resolve_profile(original, 'phrase').identity != resolve_profile(changed, 'phrase').identity


def test_unsupported_setting_is_explicit():
    config = default_profiles()
    config['open']['effort'] = 'invented'
    with pytest.raises(ProviderError) as error:
        resolve_profile(config, 'open')
    assert error.value.code == 'unsupported_configuration'


def test_profile_resolution_does_not_probe_host(monkeypatch):
    monkeypatch.setenv('PATH', '')
    assert resolve_profile(default_profiles(), 'open').provider == 'codex'


def fake_codex(tmp_path, *, failure=False):
    import json
    import sys
    path = tmp_path / 'codex'
    capture = tmp_path / 'captured.json'
    path.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
if '--version' in sys.argv:
    print('codex fixture 1'); sys.exit(0)
args = sys.argv[1:]
setting = next((value for value in args if value.startswith('model_instructions_file=')), None)
system = Path(json.loads(setting.split('=',1)[1])).read_text() if setting else None
''' + f'Path({str(capture)!r}).write_text(json.dumps({{"args":args,"system":system,"input":sys.stdin.read()}}))\n' +
        ("print(json.dumps({'type':'turn.failed','error':{'message':'fixture transport failure'}}));sys.exit(1)\n" if failure else "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\"b\":[]}'}}))\n"))
    path.chmod(0o700)
    return path, capture


def test_codex_reuses_native_message_transport_and_retains_identity(tmp_path):
    import json
    from knowledge_atlas.providers.codex import CodexProvider
    binary, capture = fake_codex(tmp_path)
    provider = CodexProvider(executable=str(binary))
    config = default_profiles(); config['phrase']['guidance'] = ''
    result = provider.structured({'operation': 'phrase', 'messages': [{'role':'system','content':'UPSTREAM SYSTEM'}, {'role':'user','content':'UPSTREAM USER'}]}, resolve_profile(config, 'phrase'), tmp_path / 'evidence')
    assert result == {'raw_response': '{"b":[]}'}
    observed = json.loads(capture.read_text())
    assert observed['system'] == 'UPSTREAM SYSTEM'
    assert observed['input'] == 'UPSTREAM USER'
    identity = json.loads((tmp_path / 'evidence/profile.json').read_text())
    assert identity['cli_version'] == 'codex fixture 1'
    assert identity['adapter_sha256']


def test_codex_failure_retains_evidence_without_retry(tmp_path):
    from knowledge_atlas.providers.codex import CodexProvider
    binary, _ = fake_codex(tmp_path, failure=True)
    provider = CodexProvider(executable=str(binary))
    with pytest.raises(ProviderError) as error:
        provider.structured({'operation':'proposal'}, resolve_profile(default_profiles(), 'proposal'), tmp_path / 'evidence')
    assert error.value.code == 'provider_failed'
    assert 'fixture transport failure' in (tmp_path / 'evidence/stderr.txt').read_text()


def test_configured_models_uses_provider_and_retains_stage_identity(tmp_path):
    import json
    from knowledge_atlas.core.model_config import ConfiguredModels
    from knowledge_atlas.providers.codex import CodexProvider
    binary, capture = fake_codex(tmp_path)
    config = default_profiles(); config['phrase']['guidance'] = ''
    models = ConfiguredModels(config=config, providers={'codex': CodexProvider(str(binary))}, evidence=tmp_path / 'calls')
    profile = models.effective_profile('phrase')
    assert profile['provider'] == 'codex'
    assert profile['cli_version'] == 'codex fixture 1'
    assert models({'operation':'phrase','messages':[{'role':'system','content':'SYSTEM'},{'role':'user','content':'USER'}]}) == {'raw_response':'{"b":[]}'}
    assert json.loads(capture.read_text())['system'] == 'SYSTEM'


def test_configured_call_rejects_cli_change_after_profile_freeze(tmp_path):
    from knowledge_atlas.core.model_config import ConfiguredModels
    from knowledge_atlas.providers.codex import CodexProvider
    binary, _ = fake_codex(tmp_path)
    models = ConfiguredModels(config=default_profiles(), providers={'codex': CodexProvider(str(binary))}, evidence=tmp_path / 'calls')
    binary.write_text(binary.read_text().replace('codex fixture 1', 'codex fixture 2'))
    with pytest.raises(ProviderError) as error:
        models({'operation':'phrase','messages':[{'role':'system','content':'SYSTEM'},{'role':'user','content':'USER'}]})
    assert error.value.code == 'provider_identity_changed'
