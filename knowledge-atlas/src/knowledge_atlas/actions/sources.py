"""Public source descriptors adapt to the retained native Wiki file boundary."""
import hashlib
from pathlib import Path
from ..contracts import AtlasError


def prepare_sources(items, remove_source_ids, previous_wiki, destination):
    if not isinstance(items, list) or not isinstance(remove_source_ids, list):
        raise AtlasError('invalid_request', 'sources and remove_source_ids must be arrays')
    previous = {row.get('source_id', row['id'] if 'id' in row else row['filename']): row
                for row in previous_wiki.get('sources', [])}
    removals = []
    for identity in remove_source_ids:
        if not isinstance(identity, str) or identity not in previous:
            raise AtlasError('unknown_source', 'explicit removal must name an existing source', 422)
        if identity in removals:
            raise AtlasError('invalid_request', 'duplicate source removal')
        removals.append(identity)
    filenames = {row['filename'].casefold(): identity for identity,row in previous.items()}
    descriptors = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in ('source_id','name','content')):
            raise AtlasError('invalid_request', 'source_id, name and UTF-8 content are required')
        identity = item['source_id']
        if not identity or identity in seen or identity in removals:
            raise AtlasError('invalid_request', 'each source ID must have one update or removal')
        if not item['name'] or Path(item['name']).name != item['name']:
            raise AtlasError('invalid_request', 'source name must be a plain filename')
        try:
            raw = item['content'].encode('utf-8')
        except UnicodeError as error:
            raise AtlasError('invalid_request', 'UTF-8 source text required') from error
        seen.add(identity)
        filename = previous[identity]['filename'] if identity in previous else hashlib.sha256(identity.encode()).hexdigest()+'.txt'
        if Path(filename).name != filename or filename in ('', '.', '..'):
            raise AtlasError('invalid_source', 'retained source filename is unsafe', 422)
        owner = filenames.get(filename.casefold())
        if owner is not None and owner != identity:
            raise AtlasError('source_name_conflict', 'internal filename belongs to another source', 409)
        filenames[filename.casefold()] = identity
        descriptors.append(({'path':str(Path(destination)/filename), 'source_id':identity, 'name':item['name'],
            **{key:item[key] for key in ('source_time','valid_from','valid_to') if key in item}}, raw))
    Path(destination).mkdir(parents=True, exist_ok=False)
    for descriptor, raw in descriptors:
        Path(descriptor['path']).write_bytes(raw)
    return {'sources':[row for row,_ in descriptors],
            'remove_sources':[previous[identity]['filename'] for identity in removals]}


def submit_sources(context):
    from ..native_pipeline import run_native
    return run_native(context)


def decide_type(context):
    from ..native_pipeline import run_native
    return run_native(context)
