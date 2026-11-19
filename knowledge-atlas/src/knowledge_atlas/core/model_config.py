"""PoC9 operation routing adapted to explicit native provider instances."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import uuid

from ..providers.config import OPERATIONS, default_profiles, prepare_request, resolve_profile
from ..providers.base import ProviderError
from ..runtime import _assets


def provider_for(name):
    if name == 'codex':
        from ..providers.codex import CodexProvider
        return CodexProvider()
    if name == 'claude':
        from ..providers.claude import ClaudeProvider
        return ClaudeProvider()
    raise ProviderError('unsupported_configuration', 'Unknown provider: ' + name)


class ConfiguredModels:
    def __init__(self, config_path=None, *, extract_effort=None, command=None,
                 config=None, providers=None, evidence=None):
        if config is not None and config_path is not None:
            raise ProviderError('unsupported_configuration', 'Choose config or config_path')
        self.config = copy.deepcopy(config if config is not None else
            json.loads(Path(config_path).read_text()) if config_path else default_profiles())
        if extract_effort:
            self.config['open']['effort'] = extract_effort
        self.stage_profiles = {operation: resolve_profile(self.config, operation) for operation in OPERATIONS}
        self.providers = dict(providers or {})
        for profile in self.stage_profiles.values():
            if profile.provider not in self.providers:
                self.providers[profile.provider] = provider_for(profile.provider)
        self.profiles = {operation: self.providers[profile.provider].identity(profile, operation)
                         for operation, profile in self.stage_profiles.items()}
        self.stage_profiles = {operation: replace(profile, identity={**profile.identity,
            'execution_identity': {key: self.profiles[operation][key] for key in ('cli_version', 'adapter_sha256')}})
            for operation, profile in self.stage_profiles.items()}
        self.evidence = Path(evidence) if evidence else Path(tempfile.mkdtemp(prefix='knowledge-atlas-models-'))
        self.transports = {}
        if command is not None:
            for operation, profile in self.stage_profiles.items():
                if profile.provider != 'codex':
                    raise ProviderError('unsupported_configuration', 'Legacy command transport is Codex-only')
                if operation != 'wiki':
                    self.transports[operation] = command(self._argv(operation))

    def _argv(self, operation):
        profile = self.stage_profiles[operation]
        if profile.provider != 'codex':
            raise ProviderError('unsupported_configuration', 'Use the native provider callback for this host')
        adapter = _assets() / ('wiki-agent-codex.py' if operation == 'wiki' else 'relation-model-codex.py')
        return [sys.executable, str(adapter), '--model', profile.model, '--effort', profile.effort]

    @property
    def wiki_argv(self):
        return self._argv('wiki')

    def effective_profile(self, operation):
        return copy.deepcopy(self.profiles[operation])

    def prepare_request(self, request):
        return prepare_request(request, self.stage_profiles[request['operation']])

    def __call__(self, request):
        operation = request['operation']
        if operation in self.transports:
            return self.transports[operation](self.prepare_request(request))
        profile = self.stage_profiles[operation]
        return self.providers[profile.provider].structured(request, profile, self.evidence / str(uuid.uuid4()))

    async def wiki(self, request):
        profile = self.stage_profiles['wiki']
        return await self.providers[profile.provider].wiki(request, profile, self.evidence / str(uuid.uuid4()))


def configured_models(config_path=None, **kwargs):
    return ConfiguredModels(config_path, **kwargs)
