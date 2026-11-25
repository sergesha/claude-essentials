"""PoC9 operation routing adapted to explicit native provider instances."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import uuid

from ..providers.config import default_profiles, prepare_request, resolve_profiles
from ..providers.base import ProviderError


def provider_for(name):
    if name == 'codex':
        from ..providers.codex import CodexProvider
        return CodexProvider()
    if name == 'claude':
        from ..providers.claude import ClaudeProvider
        return ClaudeProvider()
    raise ProviderError('unsupported_configuration', 'Unknown provider: ' + name)


class ConfiguredModels:
    def __init__(self, config_path=None, *, extract_effort=None,
                 config=None, providers=None, evidence=None):
        if config is not None and config_path is not None:
            raise ProviderError('unsupported_configuration', 'Choose config or config_path')
        self.config = copy.deepcopy(config if config is not None else
            json.loads(Path(config_path).read_text()) if config_path else default_profiles())
        if extract_effort and isinstance(self.config, dict) and isinstance(self.config.get('open'), dict):
            self.config['open']['effort'] = extract_effort
        self.stage_profiles = resolve_profiles(self.config)
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

    def effective_profile(self, operation):
        return copy.deepcopy(self.profiles[operation])

    def prepare_request(self, request):
        return prepare_request(request, self.stage_profiles[request['operation']])

    def __call__(self, request):
        operation = request['operation']
        profile = self.stage_profiles[operation]
        return self.providers[profile.provider].structured(request, profile, self.evidence / str(uuid.uuid4()))

    async def wiki(self, request):
        profile = self.stage_profiles['wiki']
        return await self.providers[profile.provider].wiki(request, profile, self.evidence / str(uuid.uuid4()))


def configured_models(config_path=None, **kwargs):
    return ConfiguredModels(config_path, **kwargs)
