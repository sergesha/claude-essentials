"""Optional candidate retrieval; native Utopia alone decides bindings."""
import copy
import time


def select_candidates(items, catalog, selector):
    if selector is None:
        return items, None
    started = time.monotonic()
    trace = {'shortlisted_indices': [], 'fallback_indices': [], 'candidates': {}}
    try:
        proposed = selector(copy.deepcopy(items), copy.deepcopy(catalog))
        if not isinstance(proposed, dict):
            raise ValueError('candidate plugin must return an item-ID mapping')
        selected = copy.deepcopy(items)
        for item in selected:
            kind = 'attribute' if item['object_is_value'] else 'relation'
            compatible = [t['id'] for t in catalog if t.get('kind', 'relation') == kind]
            ids = proposed.get(item['id'])
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                continue
            ids = list(dict.fromkeys(x for x in ids if x in compatible))
            if ids and set(ids) != set(compatible):
                item['candidate_ids'] = ids
                trace['shortlisted_indices'].append(item['id'])
                trace['candidates'][str(item['id'])] = ids
        return selected, trace
    except Exception as exc:
        # Optional retrieval cannot turn backend failure into missing knowledge.
        trace['error'] = str(exc)
        return items, trace
    finally:
        trace['elapsed_seconds'] = time.monotonic() - started
