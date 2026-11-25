"""Portable PoC9 archives, with readiness gated by native projection recovery."""
from dataclasses import replace
from pathlib import Path
from ..contracts import ActionResult, AtlasError
from ..core.archive import export_atlas, restore_atlas
from ..core.publication import Publications
from ..operations import external_archive_path, register_atlas, staged_restore_input
from ..native_pipeline import run_native
from ..project_registry import prepare_restored_project, project_configuration, project_store
from ..runtime import validate_runtime


def rebuild_restored(context):
    request = replace(context.request, action='publication.rebuild', atlas_id=context.project_binding.atlas_id)
    return run_native(replace(context, request=request))


def export_archive(context):
    destination = context.request.payload.get('path')
    if not isinstance(destination, str) or not destination:
        raise AtlasError('invalid_request', 'archive output path required')
    store = project_store(context.service, context.atlas_path)
    if Publications(store).ready() != context.request.expected_publication:
        raise AtlasError('stale_publication', 'expected publication is no longer READY', 409)
    assets = store.base/'assets'
    if assets.exists():
        tokenizer, formatter = assets/'tokenizer.json', assets/'chunking.py'
    else:
        if context.service.runtime is None:
            raise AtlasError('runtime_unavailable', 'prepared runtime assets required', 503)
        runtime = validate_runtime(context.service.runtime).runtime_root
        tokenizer, formatter = runtime/'model/tokenizer.json', runtime/'chunking.py'
    try:
        manifest = export_atlas(store, Path(destination).expanduser().resolve(), tokenizer=tokenizer, formatter=formatter)
    except (ValueError, KeyError) as error:
        raise AtlasError('invalid_archive', str(error), 422) from error
    return ActionResult(data={'path':destination, 'manifest':manifest}, publication=manifest['ready'])


def restore_archive(context):
    archive = context.request.payload.get('archive')
    if not isinstance(archive, str) or not archive:
        raise AtlasError('invalid_request', 'archive input path required')
    if context.atlas_path.exists():
        raise AtlasError('destination_exists', 'restore requires an unused destination', 409)
    try:
        source = staged_restore_input(context.evidence) if context.operation_id else external_archive_path(context.service, archive)
        store = restore_atlas(source, context.atlas_path)
    except (ValueError, KeyError, OSError) as error:
        raise AtlasError('invalid_archive', str(error), 422) from error
    # Canonical objects remain recoverable if native prerequisites fail. Never
    # register them as ready until the retained rebuild finishes successfully.
    binding = prepare_restored_project(context.service, store.base)
    configuration = project_configuration(context.service, binding)
    rebuilt = rebuild_restored(replace(context, configuration=configuration, project_binding=binding))
    identity = binding.atlas_id
    register_atlas(context.service, identity, store.base)
    return ActionResult(data={'atlas_id':identity, 'path':str(store.base), 'status':'ready',
                             'rebuild':rebuilt.data}, publication=rebuilt.data['ready'],
                        evidence=rebuilt.evidence, canonical_effects={'restored_atlas':identity})
