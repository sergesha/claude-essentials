"""Claude-specific transport for the same native Wiki/Utopia responsibilities."""
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess

from ..contracts import JsonObject, StageProfile
from ..runtime import _assets
from .. import wiki_mcp
from .base import ProviderError
from .config import prepare_request

class ClaudeProvider:
    def __init__(self, executable: str = 'claude'):
        self.executable = executable

    def identity(self, profile: StageProfile, stage: str) -> JsonObject:
        if profile.provider != 'claude':
            raise ProviderError('unsupported_configuration', 'Claude requires an explicit Claude profile')
        try:
            version = subprocess.run([self.executable, '--version'], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ProviderError('provider_unavailable', 'Claude CLI is unavailable') from error
        adapter = Path(__file__).read_bytes()
        if stage == 'wiki':
            adapter += (_assets() / 'wiki-instructions.txt').read_bytes()
            adapter += Path(wiki_mcp.__file__).read_bytes()
        execution = {'cli_version': version, 'adapter_sha256': hashlib.sha256(adapter).hexdigest()}
        expected = profile.identity.get('execution_identity')
        if expected is not None and expected != execution:
            raise ProviderError('provider_identity_changed', 'Claude identity changed after stage profile was frozen')
        return {**asdict(profile), **execution}

    def _run(self, request: JsonObject, profile: StageProfile, evidence: Path, stage: str) -> JsonObject:
        evidence = Path(evidence).resolve()
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / 'profile.json').write_text(json.dumps(asdict(profile), indent=2))
        try:
            identity = self.identity(profile, stage)
        except ProviderError as error:
            (evidence / 'error.json').write_text(json.dumps({'code': error.code, 'message': str(error)}))
            raise
        (evidence / 'profile.json').write_text(json.dumps(identity, indent=2))
        prepared = prepare_request(request, profile)
        (evidence / 'request.json').write_text(json.dumps(prepared, ensure_ascii=False, indent=2))
        command = [self.executable, '-p', '--output-format', 'stream-json', '--verbose',
                   '--no-session-persistence', '--strict-mcp-config', '--setting-sources', '',
                   '--tools', '', '--disable-slash-commands', '--permission-mode', 'dontAsk',
                   '--permission-prompts', 'none', '--no-chrome', '--model', profile.model]
        if profile.effort is not None:
            command += ['--effort', profile.effort]
        mcp = {'mcpServers': {}}
        native_messages = prepared.get('messages')
        if stage == 'wiki':
            mcp['mcpServers']['wiki'] = {key: prepared['mcp'][key] for key in ('command', 'args', 'env') if key in prepared['mcp']}
            command += ['--allowedTools', ','.join('mcp__wiki__' + name for name in wiki_mcp.WIKI_TOOLS)]
            prompt = (_assets() / 'wiki-instructions.txt').read_text() + json.dumps(
                {key: value for key, value in prepared.items() if key not in {'mcp', 'workspace'}}, ensure_ascii=False)
            command += ['--json-schema', json.dumps({'type':'object','properties':
                {'status':{'type':'string','enum':['completed','blocked']},'summary':{'type':'string'}},
                'required':['status','summary'],'additionalProperties':False})]
        elif native_messages is not None:
            if (not isinstance(native_messages, list) or len(native_messages) != 2
                    or not all(isinstance(message, dict) for message in native_messages)
                    or [message.get('role') for message in native_messages] != ['system', 'user']
                    or not all(isinstance(message.get('content'), str) for message in native_messages)):
                raise ProviderError('unsupported_configuration', 'Expected one native system then user message')
            system = evidence / 'upstream-system.txt'
            system.write_text(native_messages[0]['content'])
            command += ['--system-prompt-file', str(system)]
            prompt = native_messages[1]['content']
        else:
            prompt = 'Execute the supplied relation-processing request. Return the requested JSON serialized in response. No tools.\n' + json.dumps(prepared, ensure_ascii=False)
            command += ['--json-schema', json.dumps({'type':'object','properties':{'response':{'type':'string'}},
                'required':['response'],'additionalProperties':False})]
        mcp_path = evidence / 'mcp.json'
        mcp_path.write_text(json.dumps(mcp, ensure_ascii=False, indent=2))
        command += ['--mcp-config', str(mcp_path)]
        (evidence / 'prompt.txt').write_text(prompt)
        (evidence / 'command.json').write_text(json.dumps(command, indent=2))
        session = evidence / 'session'; session.mkdir()
        try:
            with (evidence / 'events.jsonl').open('w') as output, (evidence / 'stderr.txt').open('w') as errors:
                run = subprocess.run(command, input=prompt, text=True, stdout=output, stderr=errors, cwd=session)
        except OSError as error:
            raise ProviderError('provider_unavailable', f'Cannot start Claude; evidence: {evidence}') from error
        results = []
        for line in (evidence / 'events.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('type') == 'result':
                results.append(event)
        if run.returncode or not results or results[-1].get('is_error'):
            raise ProviderError('provider_failed', f'Claude transport failed; evidence: {evidence}')
        result = results[-1]
        if native_messages is not None:
            if not isinstance(result.get('result'), str):
                raise ProviderError('invalid_provider_response', 'Claude returned no native response')
            return {'raw_response': result['result']}
        try:
            structured = result.get('structured_output')
            if structured is None:
                structured = json.loads(result['result'])
            if stage == 'wiki':
                if structured.get('status') != 'completed':
                    raise ProviderError('provider_failed', f'Native Wiki workflow blocked; evidence: {evidence}')
                return {**structured, 'returncode': run.returncode, 'trace': str(evidence / 'events.jsonl')}
            response = json.loads(structured['response'])
            if not isinstance(response, dict):
                raise ValueError('object required')
            return response
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ProviderError('invalid_provider_response', f'Claude returned invalid JSON; evidence: {evidence}') from error

    def structured(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject:
        return self._run(request, profile, evidence, request.get('operation', 'open'))

    async def wiki(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject:
        return await asyncio.to_thread(self._run, request, profile, evidence, 'wiki')
