"""Both transports consume and freeze the same operational Wiki instructions."""
import asyncio
from dataclasses import replace
import json
import shutil

import pytest

from knowledge_atlas.providers.base import ProviderError
from knowledge_atlas.providers.config import default_profiles, resolve_profile
from knowledge_atlas.runtime import _assets
from test_codex_provider import fake_codex
from test_claude_provider import fake_claude, profiles


@pytest.fixture(params=['codex', 'claude'])
def transport(request, tmp_path, monkeypatch):
    from knowledge_atlas.providers import codex, claude
    assets = tmp_path / 'assets'
    assets.mkdir()
    shutil.copy2(_assets() / 'wiki-agent-codex.py', assets)
    instructions = assets / 'wiki-instructions.txt'
    instructions.write_text('Shared operational instruction A.\n')
    if request.param == 'codex':
        binary, capture = fake_codex(tmp_path)
        binary.write_text(binary.read_text().replace('{"b":[]}', '{"status":"completed","summary":"done"}'))
        provider = codex.CodexProvider(str(binary))
        config = default_profiles()
        monkeypatch.setattr(codex, '_assets', lambda: assets)
    else:
        binary, capture = fake_claude(tmp_path)
        binary.write_text(binary.read_text().replace('{"b":[]}', '{"status":"completed","summary":"done"}'))
        provider = claude.ClaudeProvider(str(binary))
        config = profiles()
        monkeypatch.setattr(claude, '_assets', lambda: assets)
    return provider, resolve_profile(config, 'wiki'), instructions, capture


def test_wiki_transport_delivers_shared_instructions(transport, tmp_path):
    provider, profile, instructions, capture = transport
    result = asyncio.run(provider.wiki({'workspace': str(tmp_path / 'wiki'),
        'mcp': {'command': '/native/python', 'args': ['local_server.py']},
        'sources': [{'filename': 'evidence.txt'}]}, profile, tmp_path / 'call'))
    assert result['status'] == 'completed'
    delivered = json.loads(capture.read_text())['input']
    assert delivered.startswith('Shared operational instruction A.\n')
    assert 'evidence.txt' in delivered
    assert '/native/python' not in delivered


def test_wiki_prompt_change_invalidates_frozen_execution(transport, tmp_path):
    provider, profile, instructions, capture = transport
    identity = provider.identity(profile, 'wiki')
    frozen = replace(profile, identity={**profile.identity, 'execution_identity':
        {key: identity[key] for key in ('cli_version', 'adapter_sha256')}})
    instructions.write_text('Shared operational instruction B.\n')
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.wiki({'workspace': str(tmp_path / 'wiki')}, frozen, tmp_path / 'call'))
    assert error.value.code == 'provider_identity_changed'
    assert not capture.exists()


def test_wiki_task_guidance_reaches_native_host_and_changes_identity(transport, tmp_path):
    provider, original, instructions, capture = transport
    config = default_profiles() if original.provider == 'codex' else profiles()
    config['wiki']['guidance'] = 'Maintain this Wiki using the native guide; verify source support.'
    profile = resolve_profile(config, 'wiki')
    assert profile.identity != original.identity
    asyncio.run(provider.wiki({'workspace': str(tmp_path / 'wiki'),
        'mcp': {'command': '/native/python', 'args': ['local_server.py']},
        'sources': [{'filename': 'evidence.txt'}]}, profile, tmp_path / 'call'))
    delivered = json.loads(capture.read_text())['input']
    payload = json.loads(delivered.removeprefix(instructions.read_text()))
    assert payload['guidance'] == 'Maintain this Wiki using the native guide; verify source support.'
    assert json.loads((tmp_path / 'call' / 'profile.json').read_text())['guidance'] == profile.guidance
