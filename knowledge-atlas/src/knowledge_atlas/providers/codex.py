"""Thin provider wrapper around the retained PoC9 Codex transports."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from ..contracts import JsonObject, StageProfile
from ..runtime import _assets
from .base import ProviderError
from .config import prepare_request


class CodexProvider:
    def __init__(self, executable: str = 'codex'):
        self.executable = executable

    def identity(self, profile: StageProfile, stage: str) -> JsonObject:
        if profile.provider != 'codex':
            raise ProviderError('unsupported_configuration', 'Codex requires a Codex stage profile')
        adapter = _assets() / ('wiki-agent-codex.py' if stage == 'wiki' else 'relation-model-codex.py')
        try:
            version = subprocess.run([self.executable, '--version'], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ProviderError('provider_unavailable', 'Codex CLI is unavailable') from error
        adapter_bytes = adapter.read_bytes()
        if stage == 'wiki':
            adapter_bytes += (_assets() / 'wiki-instructions.txt').read_bytes()
        execution = {'cli_version': version, 'adapter_sha256': hashlib.sha256(adapter_bytes).hexdigest()}
        expected = profile.identity.get('execution_identity')
        if expected is not None and expected != execution:
            raise ProviderError('provider_identity_changed', 'Codex identity changed after stage profile was frozen')
        return {**asdict(profile), **execution}

    def _run(self, request: JsonObject, profile: StageProfile, evidence: Path, stage: str) -> JsonObject:
        evidence = Path(evidence)
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
        adapter = _assets() / ('wiki-agent-codex.py' if stage == 'wiki' else 'relation-model-codex.py')
        command = [sys.executable, str(adapter), '--model', profile.model, '--effort', profile.effort]
        environment = {**os.environ, 'KNOWLEDGE_ATLAS_CODEX': self.executable}
        # Native credentials remain in the CLI's environment/configuration. Do not serialize it.
        try:
            with (evidence / 'stdout.txt').open('w') as output, (evidence / 'stderr.txt').open('w') as errors:
                result = subprocess.run(command, input=json.dumps(prepared, ensure_ascii=False), text=True,
                                        stdout=output, stderr=errors, env=environment)
        except OSError as error:
            raise ProviderError('provider_unavailable', 'Cannot start retained Codex transport') from error
        if result.returncode:
            raise ProviderError('provider_failed', f'Codex transport failed; evidence: {evidence}')
        try:
            response = json.loads((evidence / 'stdout.txt').read_text())
            if not isinstance(response, dict):
                raise ValueError('object required')
            return response
        except ValueError as error:
            raise ProviderError('invalid_provider_response', f'Codex returned invalid JSON; evidence: {evidence}') from error

    def structured(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject:
        return self._run(request, profile, evidence, request.get('operation', 'open'))

    async def wiki(self, request: JsonObject, profile: StageProfile, evidence: Path) -> JsonObject:
        return await asyncio.to_thread(self._run, request, profile, evidence, 'wiki')
