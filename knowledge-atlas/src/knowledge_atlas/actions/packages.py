"""Reviewed package composition using PoC9's exact Git submodule pins."""
from datetime import datetime
from pathlib import Path
import uuid

from ..contracts import ActionRequest, ActionResult, AtlasError
from ..core.git_store import GitStore, git
from ..core.publication import Publications


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
    store, child = GitStore(context.atlas_path), GitStore(Path(data['path']).expanduser())
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


def inspect_packages(context):
    from ..core.reader import AtlasReader
    store = GitStore(context.atlas_path)
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
