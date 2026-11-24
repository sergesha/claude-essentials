"""Pinned published reads with explicit current per-record synthetic permissions.

The policy callback is evaluated on each operation, including historical reads.
This adapter is not production authentication. Retrieval does not by itself
establish that a statement is applicable to the caller's case.
"""
import json
from .applicability import assess, instant
from .graph_stage import GraphStage, records_for
from .publication import Publications, acceptance_instant
from .decision_receipts import matching_decision_receipt


def short_id(value):
    return value.split('/data/')[-1]


def projection_receipt(store, publication, receipt):
    """Apply a verified local rebuild to the selected Git publication."""
    rebuilt_path = store.base / 'rebuilt-projections.json'
    if not rebuilt_path.exists():
        return dict(receipt)
    rebuilt_map = json.loads(rebuilt_path.read_text())
    if not isinstance(rebuilt_map, dict):
        raise ValueError('invalid rebuilt projection inventory')
    if publication not in rebuilt_map:
        return dict(receipt)
    rebuilt = rebuilt_map[publication]
    if (not isinstance(rebuilt, dict) or rebuilt.get('content_commit') != receipt['content_commit']
            or rebuilt.get('profile_id') != receipt['profile_id']
            or rebuilt.get('ready') is not True or not rebuilt.get('graph_commit')
            or rebuilt.get('search_checkpoint') != rebuilt['graph_commit']):
        raise ValueError('rebuilt projection does not match published content')
    return {**receipt, **{field: rebuilt[field] for field in ('database', 'graph_commit', 'search_checkpoint')}}


class AtlasReader:
    def __init__(self, store, url, policy, publication=None, *, as_known_at=None):
        if publication is not None and as_known_at is not None:
            raise ValueError('choose publication or as_known_at, not both')
        self.store, self.pubs, self.policy = store, Publications(store), policy
        self.ready_at_open = self.pubs.ready()
        self.publication = publication or self.ready_at_open
        if not self.publication:
            raise ValueError('no published slice')
        self.chain = {}
        current = self.ready_at_open
        while current:
            if current in self.chain:
                raise ValueError('publication history cycle')
            receipt = self.pubs.read(current)
            self.chain[current] = receipt
            current = receipt['previous_publication']
        when = instant(as_known_at) if as_known_at is not None else None
        selected = None
        visible_from = None
        for oid, receipt in reversed(self.chain.items()):
            # A child incorporates its ancestors and cannot be visible earlier.
            accepted = acceptance_instant(receipt)
            visible_from = max(visible_from, accepted) if visible_from is not None else accepted
            receipt.pop('effective_accepted_at', None)  # Derived from the chain at read time.
            if visible_from != instant(receipt['accepted_at']):
                receipt['effective_accepted_at'] = visible_from.isoformat()
            if when is not None and visible_from <= when:
                selected = oid
        if when is not None:
            if selected is None:
                raise ValueError('no published slice accepted by requested time')
            self.publication = selected
        if self.publication not in self.chain:
            raise ValueError('slice is not in published history')
        self.receipt = projection_receipt(store, self.publication, self.chain[self.publication])
        self.records = self._records(self.publication)
        self.graph = GraphStage(url, self.receipt['database'])

    def matching_decision_receipt(self, publication=None):
        return matching_decision_receipt(self.store, self.chain[publication or self.publication])

    def _records(self, publication):
        if publication not in self.chain:
            raise ValueError('slice is not in pinned published history')
        content = self.chain[publication]['content_commit']
        return {d['@id']: d for d in records_for(self.store.read(content), content)}

    def _visible(self, records):
        allowed = set(self.policy())
        historical = {}
        # A node is scoped to its exact document set, including pinned package
        # provenance. Traverse dependencies once, then propagate denial backwards.
        # This keeps valid cycles visible without caching an optimistic cycle
        # result that could conceal a denied descendant.
        roots = {(id(records), rid) for rid in allowed & set(records)}
        pending = [(rid, records) for _, rid in roots]
        visited, denied, dependents = set(), set(), {}
        while pending:
            rid, docs = pending.pop()
            token = (id(docs), rid)
            if token in visited:
                continue
            visited.add(token)
            if rid not in allowed or rid not in docs:
                denied.add(token)
                continue
            try:
                record = docs[rid]
                dependencies = [(short_id(ref), docs) for ref in record.get('links', [])]
                if rid.startswith('Record/knowledge-'):
                    for citation in json.loads(record['payload'])['provenance']:
                        commit = citation['source_commit']
                        package = citation.get('source_package_id')
                        source_key = (package, commit)
                        if source_key not in historical:
                            source = (self.store.read_package(package, commit, atlas_commit=self.receipt['content_commit'])
                                      if package else self.store.read(commit))
                            historical[source_key] = {d['@id']: d for d in records_for(source, commit, namespace=package)}
                        dependencies.append((citation['record_id'], historical[source_key]))
                for child, child_docs in dependencies:
                    dependents.setdefault((id(child_docs), child), set()).add(token)
                    pending.append((child, child_docs))
            except (KeyError, ValueError, OSError):
                # Unresolvable provenance cannot authorize exposing its quote.
                denied.add(token)
        pending = list(denied)
        while pending:
            for parent in dependents.get(pending.pop(), ()):
                if parent not in denied:
                    denied.add(parent)
                    pending.append(parent)
        return {rid for token, rid in roots if (token, rid) not in denied}

    def search(self, query, *, mode='hybrid', count=10, eligible=None, purpose='research'):
        if mode not in ('vector', 'fts', 'hybrid') or not isinstance(query, str) or not query.strip():
            raise ValueError('nonempty query and supported search mode required')
        if type(count) is not int or count < 1:
            raise ValueError('positive result count required')
        if purpose not in ('research', 'recommendation'):
            raise ValueError('supported search purpose required')
        visible = self._visible(self.records)
        allowed = visible if eligible is None else visible & set(eligible)
        if purpose == 'recommendation':
            allowed = {rid for rid in allowed if self.records[rid]['kind'] != 'judgment'
                       or (self.records[rid]['status'] == 'accepted'
                           and json.loads(self.records[rid]['payload']).get('adoption', 'adopted') == 'adopted')}
        base = {'publication_commit': self.publication, 'acceptance_kind': self.receipt['acceptance_kind'],
                'interpretation': 'retrieval_only', 'purpose': purpose, 'score_type': 'ranking', 'hits': []}
        if not allowed:
            return {**base, 'status': 'no_eligible_records'}
        try:
            docs = self.graph.documents(self.receipt['graph_commit'])
            chunks = {short_id(d['@id']): d for d in docs
                      if d['@type'] == 'Chunk' and short_id(d['record']) in allowed}
            if not chunks:
                raise ValueError('published eligible records have no chunks')
            # Native HTTP permits at most 1000 results per page. Keep the
            # complete prefilter and selected commit across offset pages;
            # repeated chunks must not consume the record-level result count.
            hits = [];seen = set();offset = 0
            while offset < len(chunks):
                page_count = min(1000, len(chunks) - offset)
                body = {'q': query, 'mode': mode, 'start': offset, 'count': page_count,
                        'doc_id': sorted(chunks), 'exact_commit': True}
                response = self.graph.call('POST', '/api/search/' + self.graph.database + '/local/commit/' + self.receipt['graph_commit'], body)
                self.graph.trace[-1]['request_body'] = body
                if response['headers'].get('terminusdb-data-version') != 'commit:' + self.receipt['graph_commit']:
                    raise ValueError('search served commit mismatch')
                page = response['body']
                for hit in page:
                    cid = short_id(hit['id'])
                    if cid not in chunks:
                        raise ValueError('native search returned an ineligible chunk')
                    chunk = chunks[cid];rid = short_id(chunk['record'])
                    if rid not in seen:
                        hits.append({'record': self.records[rid], 'distance': hit.get('distance'),
                                     'matched_span': {'start': chunk['start'], 'end': chunk['end']}})
                        seen.add(rid)
                if len(hits) >= count or len(page) < page_count:
                    break
                offset += page_count
            return {**base, 'status': 'matches' if hits else 'no_matches', 'hits': hits[:count],
                    'graph_commit': self.receipt['graph_commit']}
        except (RuntimeError, ValueError, OSError):
            return {**base, 'status': 'technical_error', 'reason': 'exact search projection unavailable or inconsistent'}

    def retrieve_evidence(self, record_id):
        if record_id not in self._visible(self.records):
            return None
        return {'publication_commit': self.publication, 'record': self.records[record_id],
                'acceptance_kind': self.receipt['acceptance_kind']}

    def answer(self, record_id, context, *, valid_at=None):
        visible = self._visible(self.records)
        if record_id not in visible:
            return None
        semantic = {rid: json.loads(r['payload']) for rid,r in self.records.items()
                    if rid in visible and rid.startswith('Record/knowledge-')}
        if record_id not in semantic:
            return {'status': 'retrieval_only', 'result': None, 'publication_commit': self.publication}
        base = {'publication_commit': self.publication, 'graph_commit': self.receipt['graph_commit'],
                'acceptance_kind': self.receipt['acceptance_kind']}
        try:
            # An application answer must use the exact published graph as well
            # as its canonical Git content, never a current-branch substitute.
            docs = self.graph.documents(self.receipt['graph_commit'])
            projected = {short_id(d['@id']): d for d in docs if d['@type'] == 'Record'}
            if any(projected.get(rid, {}).get('payload') != self.records[rid]['payload'] for rid in semantic):
                raise ValueError('semantic graph/Git mismatch')
            evaluation = assess(semantic, record_id, context, valid_at=valid_at)
            record = semantic[record_id]
            conflicts = evaluation['conflicts']
            status = evaluation['status']
            grounds = [];visited = {record_id}
            def collect(rid):
                for group in semantic[rid].get('support_sets', []):
                    for child in group:
                        if child in semantic and child not in visited:
                            visited.add(child);grounds.append(semantic[child]);collect(child)
            collect(record_id)
            unknowns = list(evaluation['missing_conditions'])
            questions = ['Confirm '+', '.join(unknowns)] if unknowns else []
            if status == 'insufficient' and 'required_ground_unavailable' in evaluation['reasons']:
                missing = evaluation['missing_grounds']
                unknowns += ['Required ground: '+rid for rid in missing]
                if missing:
                    questions.append('Provide reviewed evidence for required grounds unavailable in this publication: '+', '.join(missing))
            return {**base, 'status': status, 'result': record['text'] if status in ('supported', 'conditional') else None,
                    'conditions': record.get('conditions', {}), 'grounds': grounds,
                    'experience': evaluation['independent_episodes'], 'unknowns': unknowns,
                    'clarification': '; '.join(questions) if questions else None,
                    'reasons': evaluation['reasons'], 'conflicts': [semantic[rid] for rid in conflicts],
                    'provenance': record['provenance'], 'assessment': evaluation,
                    'declared_opposition': evaluation['declared_opposition'],
                    'revision_reason': record.get('reason')}
        except (RuntimeError, ValueError, OSError):
            return {**base, 'status': 'technical_error', 'result': None,
                    'reason': 'exact application projection unavailable or inconsistent'}

    def graph_neighborhood(self, record_id, *, depth=None, result_limit=None):
        bounded = depth is not None or result_limit is not None
        if depth is not None and (type(depth) is not int or depth < 0):
            raise ValueError('nonnegative integer depth required')
        if result_limit is not None and (type(result_limit) is not int or result_limit < 1):
            raise ValueError('positive integer result limit required')
        visible = self._visible(self.records)
        if record_id not in visible:
            return None
        try:
            docs = self.graph.documents(self.receipt['graph_commit'])
            graph = {short_id(d['@id']): d for d in docs if d['@type'] == 'Record'}
            if bounded and any(graph.get(rid, {}).get('payload') != self.records[rid]['payload'] for rid in visible):
                raise ValueError('graph/Git mismatch')
        except (RuntimeError, ValueError, OSError):
            if not bounded:
                raise
            return {'publication_commit': self.publication, 'status': 'technical_error',
                    'complete': False, 'absence_proven': False,
                    'reason': 'exact graph projection unavailable or inconsistent'}
        outgoing = {short_id(x) for x in graph[record_id].get('links', [])} & visible
        incoming = {rid for rid,d in graph.items() if record_id in map(short_id, d.get('links', []))} & visible
        if bounded:
            max_depth = 1 if depth is None else depth
            limit = len(visible) if result_limit is None else result_limit
            adjacency = {rid: {} for rid in visible}
            edges = []
            def connect(edge):
                left, right = edge['from'], edge['to']
                if left not in visible or right not in visible:
                    return
                edges.append(edge)
                for source, target in ((left, right), (right, left)):
                    previous = adjacency[source].get(target)
                    if previous is None or previous['representation'] == 'native_link':
                        adjacency[source][target] = edge
            payloads = {rid: json.loads(graph[rid]['payload']) for rid in visible}
            local_ids = {(p.get('package_id'), p['local_id']): rid for rid,p in payloads.items()
                         if isinstance(p.get('local_id'), str)}
            for rid in sorted(visible):
                for target in map(short_id, graph[rid].get('links', [])):
                    connect({'id': rid, 'from': rid, 'to': target, 'type': 'links',
                             'representation': 'native_link'})
                payload = payloads[rid]
                if (graph[rid]['kind']=='attribute' and graph[rid]['status']=='accepted'
                        and payload.get('status')=='resolved' and 'subject' in graph[rid]):
                    connect({'id':rid,'from':short_id(graph[rid]['subject']),'to':rid,
                             'type':payload['type_id'],'representation':'controlled_attribute',
                             'value':payload['value'],'phrase':payload['phrase'],
                             'definition':payload['type_definition'],
                             **{f:payload[f] for f in ('polarity','details','qualifiers','when','ended') if f in payload}})
                if (graph[rid]['kind']=='relation' and graph[rid]['status']=='accepted'
                        and payload.get('status')=='resolved' and 'subject' in graph[rid] and 'object' in graph[rid]):
                    connect({'id': rid, 'from': short_id(graph[rid]['subject']),
                             'to': short_id(graph[rid]['object']), 'type': payload['type_id'],
                             'polarity':payload['polarity'], 'details':payload['details'],
                             'qualifiers':payload['qualifiers'], 'when':payload['when'],
                             'value':payload['value'], 'definition':payload['type_definition'],
                             'representation': 'controlled_relation',
                             **{f:payload[f] for f in ('ended',) if f in payload}})
                if (graph[rid]['status']=='accepted' and payload.get('relation_type_definition',{}).get('id')==payload.get('type')
                        and all(isinstance(payload.get(f), str) and payload[f] for f in ('from', 'to', 'type'))):
                    def endpoint(ref):
                        return ref if ref.startswith('Record/') else local_ids.get((payload.get('package_id'), ref))
                    connect({'id': rid, 'from': endpoint(payload['from']), 'to': endpoint(payload['to']),
                             'type': payload['type'], 'representation': 'retained_relation',
                             'definition':payload['relation_type_definition'],
                             'polarity':payload.get('polarity','unknown'),
                             **{f:payload[f] for f in ('conditions','qualifiers','valid_time','when','ended','value','details') if f in payload}})
            # Paths may walk incoming edges, but never reverse their stored roles.
            # Relation IDs reference their full retained records, not endorsement.
            selected = [record_id]
            seen = {record_id}
            paths = {record_id: {'snapshot': self.publication, 'nodes': [record_id], 'edges': []}}
            queue = [(record_id, 0)]
            truncated = False
            for rid, distance in queue:
                if distance >= max_depth:
                    continue
                for target in sorted(adjacency[rid]):
                    if target in seen:
                        continue
                    if len(selected) == limit:
                        truncated = True
                        break
                    seen.add(target)
                    selected.append(target)
                    paths[target] = {'snapshot': self.publication,
                                     'nodes': paths[rid]['nodes'] + [target],
                                     'edges': paths[rid]['edges'] + [adjacency[rid][target]]}
                    queue.append((target, distance + 1))
                if truncated:
                    break
            return {'publication_commit': self.publication, 'graph_commit': self.receipt['graph_commit'],
                    'status': 'partial' if truncated else 'ok', 'ids': selected,
                    'records': [self.records[rid] for rid in selected],
                    'edges': [edge for edge in edges if edge['from'] in seen and edge['to'] in seen],
                    'paths': [paths[rid] for rid in selected],
                    'depth': max_depth, 'truncated': truncated, 'complete': not truncated,
                    'completeness_scope': 'requested depth in the current visible graph',
                    'absence_proven': False}
        return {'publication_commit': self.publication, 'record': self.records[record_id],
                'outgoing': [self.records[rid] for rid in sorted(outgoing)],
                'incoming': [self.records[rid] for rid in sorted(incoming)]}

    def history(self, record_id):
        result = [];current = self.publication
        while current:
            records = self._records(current)
            if record_id in self._visible(records):
                receipt = self.chain[current]
                entry = {'publication_commit': current, 'accepted_at': receipt['accepted_at'],
                         'acceptance_kind': receipt['acceptance_kind'], 'acceptance_channel': receipt['acceptance_channel'],
                         'record': records[record_id], 'matching_decision_receipt': self.matching_decision_receipt(current)}
                if 'effective_accepted_at' in receipt:
                    entry['effective_accepted_at'] = receipt['effective_accepted_at']
                result.append(entry)
            current = self.chain[current]['previous_publication']
        return result

    def diff(self, before, after, record_id=None):
        left, right = self._records(before), self._records(after)
        left_visible, right_visible = self._visible(left), self._visible(right)
        result = []
        for rid in sorted(set(left) | set(right)):
            if record_id is not None and rid != record_id:
                continue
            if (rid in left and rid not in left_visible) or (rid in right and rid not in right_visible):
                continue
            if left.get(rid) != right.get(rid):
                result.append({'id': rid, 'before': left.get(rid), 'after': right.get(rid)})
        return result
