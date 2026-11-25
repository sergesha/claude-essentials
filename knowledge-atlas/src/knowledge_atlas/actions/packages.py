"""Reviewed package composition using PoC9's exact Git submodule pins."""
from datetime import datetime
from pathlib import Path
import uuid
import os
import json
from urllib.parse import urlsplit

from ..contracts import ActionContext, ActionRequest, ActionResult, AtlasError
from ..project_registry import project_store, resolve_project
from ..core.git_store import git
from ..core.publication import Publications, StaleProposal
from ..core.composition import composition_diff, configure_composition


def _compatible(nodes):
    versions = {}
    for node in nodes:
        identity, oid = node['package_id'], node['commit']
        if identity in versions and versions[identity] != oid:
            raise ValueError('conflicting pins for package identity: ' + identity)
        versions[identity] = oid


def pin_package(context):
    data = context.request.payload
    for key in ('path', 'package_id', 'commit', 'mount', 'recorded_at'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise AtlasError('invalid_request', key + ' is required')
    try:
        uuid.UUID(data['package_id'])
        if datetime.fromisoformat(data['recorded_at'].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('timezone required')
    except ValueError as error:
        raise AtlasError('invalid_request', 'valid package UUID and timezone-aware recorded_at required') from error
    ActionRequest('packages.pin', publication=data['commit'])
    if data.get('candidate') is not None: ActionRequest('candidate.inspect', publication=data['candidate'])
    if data.get('scope', 'all') != 'all':
        raise AtlasError('unsupported_scope', 'only whole-package all scope is supported', 422)
    store, child = project_store(context.service, context.atlas_path), project_store(context.service, Path(data['path']).expanduser())
    pubs = Publications(store)
    if pubs.ready() != context.request.expected_publication:
        raise AtlasError('stale_publication', 'READY changed; revalidate the package pin', 409)
    try:
        base = data.get('candidate') or (pubs.read(context.request.expected_publication)['content_commit']
            if context.request.expected_publication else git(store.root, 'rev-parse', 'HEAD'))
        child_nodes = child.closure(data['commit'])
        if child_nodes[0]['package_id'] != data['package_id']:
            raise ValueError('package identity mismatch')
        mount = data['mount']
        # Remove only the replaced inclusion from the proposed closure. A shared
        # identity reached elsewhere still constrains its version.
        parent_nodes = store.closure(base)
        if parent_nodes[0]['package_id'] in {node['package_id'] for node in child_nodes}:
            raise ValueError('package identity cycle')
        retained = [node for node in parent_nodes if any(
            inclusion != mount and not inclusion.startswith(mount + '/') for inclusion in node['mounts'])]
        _compatible(retained + child_nodes)
        candidate = store.pin_package(base, child, data['commit'], mount=mount,
            recorded_at=data['recorded_at'], base_publication=context.request.expected_publication)
    except (ValueError, OSError, KeyError) as error:
        raise AtlasError('invalid_package', str(error), 422) from error
    return ActionResult(data={'candidate':candidate, 'status':'pending_review'}, publication=pubs.ready(),
        evidence=[str(context.evidence)], canonical_effects={'candidate':candidate})


def _resolve_include(service, parent, selection):
    binding = resolve_project(service, selection['package_id'])
    child = project_store(service, binding.path)
    publication = selection['publication']
    content = Publications(child).read(publication)['content_commit']
    if json.loads(git(child.root, 'show', content + ':package.json'))['package_id'] != binding.atlas_id:
        raise ValueError('child publication belongs to another UUID')
    url = binding.git_url or os.path.relpath(child.root, parent.root)
    address = urlsplit(url)
    if (address.password or address.query or address.fragment
            or (address.scheme in ('http', 'https') and address.username)):
        raise ValueError('child Git locator must be credential-free')
    if binding.git_url and not address.scheme and ':' not in url.split('/')[0] and not Path(url).is_absolute():
        # A relative origin belongs to the child's repository, not the parent's origin.
        url = str((child.root / url).resolve())
    return {'kind': 'atlas', 'scope': 'all', 'package_id': binding.atlas_id, 'mount': selection['mount'],
            'publication': publication, 'commit': content, 'url': url}


def configure_packages(context: ActionContext) -> ActionResult:
    request = context.request
    store = project_store(context.service, context.atlas_path)
    pubs = Publications(store)
    if pubs.ready() != request.expected_publication:
        raise AtlasError('stale_publication', 'READY changed; revalidate the layout', 409)
    try:
        base = (pubs.read(request.expected_publication)['content_commit'] if request.expected_publication
                else git(store.root, 'rev-parse', 'HEAD'))
        before = json.loads(git(store.root, 'show', base + ':manifest.json'))['modules']
        resolved = [_resolve_include(context.service, store, selection) for selection in request.payload['includes']]
        changes = composition_diff(before.values(), resolved)
        candidate = configure_composition(store, base, resolved, recorded_at=datetime.now().astimezone().isoformat(),
                                          base_publication=request.expected_publication)
    except StaleProposal as error:
        raise AtlasError('stale_publication', str(error), 409) from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise AtlasError('invalid_package', str(error), 422) from error
    return ActionResult(data={'candidate': candidate, 'status': 'pending_review', 'composition_diff': changes},
                        publication=request.expected_publication, evidence=[str(context.evidence)],
                        canonical_effects={'candidate': candidate})


def inspect_packages(context):
    from ..core.reader import AtlasReader
    store = project_store(context.service, context.atlas_path)
    try:
        reader = AtlasReader(store, '', lambda: (), context.request.publication,
                             as_known_at=context.request.as_known_at)
        content = reader.receipt['content_commit']
        nodes = store.closure(content)
        _compatible(nodes)
    except (ValueError, OSError, KeyError) as error:
        raise AtlasError('invalid_package', str(error), 422) from error
    return ActionResult(data={'content':content, 'packages':[
        {**node, 'path':str(node['path'])} for node in nodes]}, publication=reader.publication)
