"""PoC9 proposal_records fixture; exact source provenance in fixtures/manifest.json."""
from knowledge_atlas.core.graph_stage import key

def proposal_records(snapshot):
    source = snapshot['sources']['review.txt'].decode()
    quote = source.split('\n')[0]
    provenance = [{'record_id': 'Record/'+key('source', 'review.txt'),
                   'start': 0, 'end': len(quote), 'quote': quote}]
    conditions = {'feedstock': 'dried', 'enclosure': 'closed', 'ambient_c': {'min': 20, 'max': 24}}
    return [
        {'id': 'p14', 'kind': 'episode', 'text': 'P-14 favored Luma for warping under the stated conditions.',
         'episode_id': 'P-14', 'conditions': conditions, 'provenance': provenance},
        {'id': 'p22', 'kind': 'episode', 'text': 'Independent P-22 favored Luma for warping under the same conditions.',
         'episode_id': 'P-22', 'conditions': conditions, 'provenance': provenance},
        {'id': 'warping', 'kind': 'lesson', 'text': 'Prefer Luma for QL-7 warping only under the stated trial conditions; no strength guarantee.',
         'conditions': conditions, 'support_sets': [['p14'], ['p22']], 'provenance': provenance},
    ]
