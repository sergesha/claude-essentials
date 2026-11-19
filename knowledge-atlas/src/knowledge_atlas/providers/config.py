"""PoC9 stage profiles with explicit provider selection and no CLI discovery."""
import copy
import hashlib
import json
from pathlib import Path

from ..contracts import JsonObject, StageProfile
from ..runtime import _assets
from .base import ProviderError

OPERATIONS = ('wiki', 'open', 'phrase', 'proposal', 'review_extension', 'grounded_answer')


def default_profiles() -> JsonObject:
    profiles = json.loads((_assets() / 'model-profiles.json').read_text())
    return {stage: {'provider': 'codex', **value} for stage, value in profiles.items()}


def resolve_profile(config: JsonObject, stage: str) -> StageProfile:
    if stage not in OPERATIONS or stage not in config:
        raise ProviderError('unsupported_configuration', 'missing or unsupported stage: ' + stage)
    settings = config[stage]
    if not isinstance(settings, dict) or set(settings) - {'provider', 'model', 'effort', 'guidance'}:
        raise ProviderError('unsupported_configuration', 'unknown stage settings')
    provider = settings.get('provider', 'codex')
    model = settings.get('model')
    effort = settings.get('effort')
    guidance = settings.get('guidance', '')
    efforts = {'codex': {'low', 'medium', 'high', 'xhigh'}, 'claude': {None, 'low', 'medium', 'high', 'max'}}
    if (provider not in efforts or not isinstance(model, str) or not model.strip()
            or effort not in efforts[provider] or not isinstance(guidance, str)):
        raise ProviderError('unsupported_configuration', 'invalid provider/model/effort/guidance for ' + stage)
    if (provider == 'claude' and model.startswith('gpt-')) or (provider == 'codex' and model.startswith('claude-')):
        raise ProviderError('unsupported_configuration', 'model belongs to another provider')
    effective = {'provider': provider, 'model': model, 'effort': effort, 'guidance': guidance}
    identity = {'config_sha256': hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'routing_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return StageProfile(**effective, identity=identity)


def prepare_request(request: JsonObject, profile: StageProfile) -> JsonObject:
    """Retained ConfiguredModels.prepare_request semantics, independent of host."""
    prepared = copy.deepcopy(request)
    if profile.guidance:
        if 'messages' in prepared:
            system = prepared['messages'][0]
            if system['role'] != 'system':
                raise ProviderError('unsupported_configuration', 'Native messages must start with system instructions')
            system['content'] += '\n\nAdditional semantic guidance:\n' + profile.guidance
        else:
            prepared['guidance'] = profile.guidance
    return prepared
