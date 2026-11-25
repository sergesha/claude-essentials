"""Claude transport must be independent of the Codex executable and flags."""
import json
import sys
import pytest
from knowledge_atlas.providers.base import ProviderError
from knowledge_atlas.providers.config import default_profiles, resolve_profile
from knowledge_atlas.providers.claude import ClaudeProvider
from knowledge_atlas.core.model_config import ConfiguredModels


def profiles():
    return {stage: {**value, 'provider':'claude', 'model':'sonnet', 'effort':'high'} for stage,value in default_profiles().items()}


def fake_claude(tmp_path, failure=False):
    binary = tmp_path / 'claude'
    capture = tmp_path / 'captured.json'
    binary.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
if '--version' in sys.argv:
    print('Claude fixture 1');sys.exit(0)
args=sys.argv[1:]
system=Path(args[args.index('--system-prompt-file')+1]).read_text() if '--system-prompt-file' in args else None
''' + f'Path({str(capture)!r}).write_text(json.dumps({{"args":args,"system":system,"input":sys.stdin.read()}}))\n' +
        ("print(json.dumps({'type':'result','is_error':True,'result':'fixture Claude failure'}));sys.exit(1)\n" if failure else "print(json.dumps({'type':'result','is_error':False,'result':'{\"b\":[]}'}))\n"))
    binary.chmod(0o700)
    return binary,capture


def test_claude_path_never_executes_codex(tmp_path, monkeypatch):
    binary,capture=fake_claude(tmp_path)
    monkeypatch.setenv('PATH', '')
    config=profiles();config['phrase']['guidance']=''
    model=ConfiguredModels(config=config, providers={'claude':ClaudeProvider(str(binary))}, evidence=tmp_path/'calls')
    response=model({'operation':'phrase','messages':[{'role':'system','content':'UPSTREAM SYSTEM'},{'role':'user','content':'UPSTREAM USER'}]})
    assert response == {'raw_response':'{"b":[]}'}
    observed=json.loads(capture.read_text())
    assert observed['system']=='UPSTREAM SYSTEM'
    assert observed['input']=='UPSTREAM USER'
    assert '--bare' not in observed['args']
    assert '--model' in observed['args']
    assert model.effective_profile('phrase')['cli_version']=='Claude fixture 1'


def test_claude_stage_settings_are_not_codex_aliases():
    config=profiles();config['phrase']['model']='gpt-6.1-sol'
    with pytest.raises(ProviderError) as error:
        resolve_profile(config,'phrase')
    assert error.value.code=='unsupported_configuration'


def test_claude_transport_failure_retains_stage_evidence(tmp_path):
    binary,_=fake_claude(tmp_path,failure=True)
    folder=tmp_path/'failure'
    with pytest.raises(ProviderError) as error:
        ClaudeProvider(str(binary)).structured({'operation':'proposal'},resolve_profile(profiles(),'proposal'),folder)
    assert error.value.code=='provider_failed'
    assert 'fixture Claude failure' in (folder/'events.jsonl').read_text()
    assert json.loads((folder/'profile.json').read_text())['cli_version']=='Claude fixture 1'


def test_claude_wiki_retains_native_mcp_and_completion(tmp_path, monkeypatch):
    import asyncio
    binary, capture = fake_claude(tmp_path)
    binary.write_text(binary.read_text().replace(
        "'result':'{\"b\":[]}'", "'structured_output':{'status':'completed','summary':'updated native page'}"))
    monkeypatch.setenv('PATH', '')
    request = {'mcp': {'command': '/runtime/python', 'args': ['local_server.py'],
                       'env': {'WIKI_ROOT': '/atlas/wiki'}},
               'workspace': '/atlas', 'sources': [{'id': 'source-a'}]}
    models = ConfiguredModels(config=profiles(), providers={'claude': ClaudeProvider(str(binary))},
                             evidence=tmp_path / 'wiki-calls')
    result = asyncio.run(models.wiki(request))
    evidence, = (tmp_path / 'wiki-calls').iterdir()
    assert result['status'] == 'completed'
    assert result['summary'] == 'updated native page'
    invocation = json.loads(capture.read_text())
    args = invocation['args']
    mcp = json.loads((evidence / 'mcp.json').read_text())
    assert mcp == {'mcpServers': {'wiki': request['mcp']}}
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--allowedTools') + 1] == 'mcp__wiki__guide,mcp__wiki__search,mcp__wiki__read,mcp__wiki__create,mcp__wiki__edit,mcp__wiki__append,mcp__wiki__lint'
    assert 'source-a' in invocation['input']
    assert '/runtime/python' not in invocation['input']
    assert '--bare' not in args


def test_wiki_mcp_advertises_only_registered_authoring_handlers():
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from knowledge_atlas.wiki_mcp import WIKI_TOOLS, restrict_tools

    server = FastMCP('fixture')
    for name in (*WIKI_TOOLS, 'administrative_fixture'):
        server.add_tool(lambda: 'fixture result', name=name)

    async def inventory():
        await restrict_tools(server)
        return [tool.name for tool in await server.list_tools()]

    assert set(asyncio.run(inventory())) == set(WIKI_TOOLS)
