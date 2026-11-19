"""Prepare an unpublished graph's model-profile chunks before native indexing."""
from .vector_chunks import chunks_for


def chunk_schema():
    return {'@id': 'Chunk', '@type': 'Class', '@key': {'@type': 'Lexical', '@fields': ['key']},
        'key': 'xsd:string', 'text': 'xsd:string', 'profile_id': 'xsd:string', 'record': 'Record',
        'ordinal': 'xsd:integer', 'start': 'xsd:integer', 'end': 'xsd:integer',
        '@metadata': {'embedding': {'query': 'query ($id: ID) { Chunk(id: $id) { text } }', 'template': '{{text}}'}}}


def prepare_chunks(graph, commit, profile, tokenizer, formatter):
    records = graph.documents(commit)
    chunks = chunks_for(records, profile, tokenizer, formatter)
    schema_path = '/api/document/' + graph.database + '?graph_type=schema'
    schema = graph.call('GET', schema_path + '&as_list=true')
    if schema['headers'].get('terminusdb-data-version') != 'branch:' + commit:
        raise ValueError('candidate branch moved before chunk preparation')
    record_schema = next(s for s in schema['body'] if s.get('@id') == 'Record')
    record_schema.pop('@metadata', None)
    # Preserve readable records; only exact-profile chunks are embedding inputs.
    graph.call('PUT', schema_path + '&author=poc9&message=profile-chunks', [record_schema])
    graph.call('POST', schema_path + '&author=poc9&message=profile-chunks', [chunk_schema()])
    result = graph.call('POST', '/api/document/' + graph.database + '?author=poc9&message=profile-chunks', chunks)
    final_commit = result['headers']['terminusdb-data-version'].split(':')[-1]
    actual = graph.documents(final_commit)
    if {d['@id']: d for d in actual if d['@type'] == 'Record'} != {d['@id']: d for d in records}:
        raise ValueError('record data changed while adding chunks')
    if {d['key'] for d in actual if d['@type'] == 'Chunk'} != {d['key'] for d in chunks}:
        raise ValueError('chunk membership mismatch')
    return {'source_graph_commit': commit, 'graph_commit': final_commit,
            'database': graph.database, 'record_count': len(records), 'chunk_count': len(chunks),
            'published': False}
