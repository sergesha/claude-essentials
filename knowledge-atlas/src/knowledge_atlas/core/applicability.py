"""Evaluate explicit reviewed grounds and conditions, not truth or causality.

Each support set is an AND; sets are OR alternatives. Callers pass only records
they may read. Missing grounds and cycles cannot manufacture support. Known valid
intervals are [from, to); null time remains unknown. Acceptance time is not valid
time. Episode IDs deduplicate evidence, never become confidence probabilities.
"""
from datetime import datetime, timezone


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if len(value) == 10:
        return parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        raise ValueError('temporal instants require a timezone')
    return parsed


def _dependency_components(records):
    """Find strongly connected components of visible support and opposition edges."""
    outgoing = {}
    incoming = {rid: [] for rid in records}
    for rid, record in records.items():
        children = {child for group in record.get('support_sets', []) for child in group}
        children.update(record.get('conflicts_with', []))
        outgoing[rid] = [child for child in children if child in records]
        for child in outgoing[rid]:
            incoming[child].append(rid)

    # Kosaraju's two iterative searches also handle long, unrelated chains.
    seen = set()
    finished = []
    for root in records:
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            rid, expanded = stack.pop()
            if expanded:
                finished.append(rid)
            elif rid not in seen:
                seen.add(rid)
                stack.append((rid, True))
                stack.extend((child, False) for child in outgoing[rid] if child not in seen)

    components = {}
    for root in reversed(finished):
        if root in components:
            continue
        members = {root}
        components[root] = members
        pending = [root]
        while pending:
            for parent in incoming[pending.pop()]:
                if parent not in components:
                    members.add(parent)
                    components[parent] = members
                    pending.append(parent)
    return components


def assess(records, record_id, context, *, valid_at=None):
    def result(status, *, missing=(), episodes=(), usable=(), reasons=(), conflicts=(), missing_grounds=()):
        return {'status': status, 'missing_conditions': sorted(set(missing)),
                'independent_episodes': sorted(set(episodes)), 'usable_support_sets': list(usable),
                'reasons': list(reasons), 'conflicts': sorted(set(conflicts)),
                'missing_grounds': sorted(set(missing_grounds))}

    components = _dependency_components(records)
    cached = {}

    def visit(rid, path):
        # Only ancestors in the same component can be reached again. On entry
        # from another component the result is independent of the incoming path.
        component = components.get(rid)
        if component is not None and not component.isdisjoint(path):
            return with_conflicts(rid, path)
        if rid not in cached:
            cached[rid] = with_conflicts(rid, frozenset())
        return cached[rid]

    def with_conflicts(rid, path):
        evaluation=evaluate(rid,path)
        if evaluation['status']=='supported':
            # Check each attributed opposing position on its own grounds;
            # a reciprocal conflict is not a recursive supporting premise.
            conflicts=[other for other in records[rid].get('conflicts_with',[])
                       if evaluate(other,path|{rid})['status']=='supported']
            if conflicts:
                return {**evaluation,'status':'conflict','conflicts':sorted(set(conflicts)),
                        'reasons':['conflicting_record']}
        return evaluation

    def evaluate(rid, path):
        if rid not in records:
            return result('insufficient', reasons=['required_ground_unavailable'], missing_grounds=[rid])
        if rid in path:
            return result('insufficient', reasons=['cyclic_ground'])
        record = records[rid]
        if record.get('status') == 'withdrawn':
            return result('withdrawn', reasons=['withdrawn_record'])
        if record.get('status') != 'accepted':
            return result('needs_review', reasons=['record_not_accepted'])
        if record.get('kind') == 'judgment' and record.get('adoption', 'adopted') != 'adopted':
            disposition = 'attributed_only' if record.get('adoption') == 'attributed_only' else 'undetermined'
            return result(disposition, reasons=['judgment_not_adopted'])
        if record.get('kind') == 'gap':
            return result('unknown', missing=record.get('missing_conditions', []), reasons=['declared_gap'])
        missing = []
        for field, expected in record.get('conditions', {}).items():
            actual = context.get(field)
            if actual is None or expected is None or actual == 'unknown' or expected == 'unknown':
                missing.append(field)
                continue
            if isinstance(expected, dict):
                if not expected or set(expected) - {'min', 'max'} or type(actual) not in (int, float):
                    missing.append(field)
                    continue
                bounds = [v for v in expected.values() if v is not None]
                if len(bounds) != len(expected) or not all(type(v) in (int, float) for v in bounds):
                    missing.append(field)
                    continue
                fits = ('min' not in expected or actual >= expected['min']) and ('max' not in expected or actual <= expected['max'])
            else:
                fits = actual == expected
            if not fits:
                return result('inapplicable', reasons=['condition_mismatch:' + field])
        if valid_at is not None:
            validity = record.get('valid_time')
            if not validity:
                missing.append('valid_time')
            elif validity.get('atemporal') is not True:
                when = instant(valid_at)
                if validity.get('from') and when < instant(validity['from']):
                    return result('inapplicable', reasons=['before_valid_interval'])
                if validity.get('to') and when >= instant(validity['to']):
                    return result('inapplicable', reasons=['after_valid_interval'])
                if not validity.get('from') or not validity.get('to'):
                    missing.append('valid_time')
        groups = record.get('support_sets', [])
        if not groups:
            if record.get('kind') == 'lesson':
                return result('insufficient', missing=missing, reasons=['lesson_has_no_explicit_grounds'])
            episodes = [record['episode_id']] if record.get('episode_id') else []
            return result('conditional' if missing else 'supported', missing=missing, episodes=episodes)
        evaluations = [[visit(child, path | {rid}) for child in dict.fromkeys(group)] for group in groups]
        usable = [i for i, group in enumerate(evaluations) if group and all(r['status'] == 'supported' for r in group)]
        if usable:
            episodes = {e for i in usable for r in evaluations[i] for e in r['independent_episodes']}
            return result('conditional' if missing else 'supported', missing=missing, episodes=episodes, usable=usable)
        possible = [group for group in evaluations if group and all(r['status'] in ('supported', 'conditional') for r in group)]
        if possible:
            missing += [field for group in possible for r in group for field in r['missing_conditions']]
            return result('conditional', missing=missing, reasons=['conditions_unconfirmed_in_grounds'])
        pending = any(group and all(r['status'] in ('supported', 'conditional', 'needs_review') for r in group) for group in evaluations)
        missing += [field for group in evaluations for r in group for field in r['missing_conditions']]
        reasons = sorted({reason for group in evaluations for r in group for reason in r['reasons']})
        conflicts={other for group in evaluations for r in group for other in r['conflicts']}
        if conflicts and not pending:
            return result('conflict',missing=missing,conflicts=conflicts,
                          reasons=['conflicting_ground',*reasons])
        return result('needs_review' if pending else 'insufficient', missing=missing,
                      reasons=['no_usable_support_set', *reasons],
                      missing_grounds=[rid for group in evaluations for r in group for rid in r['missing_grounds']])

    evaluated = visit(record_id, set())
    outbound = set(records.get(record_id, {}).get('conflicts_with', []))
    opposition = []
    for other, entry in sorted(records.items()):
        directions = []
        if record_id in entry.get('conflicts_with', []):
            directions.append('inbound')
        if other in outbound:
            directions.append('outbound')
        for direction in directions:
            opposition.append({'id': other, 'direction': direction, 'status': evaluate(other, set())['status']})
    return {**evaluated, 'declared_opposition': opposition}


def inspect_episodes(records, record_ids):
    """Inspect supplied episode identities without accepting records.

    Callers supply only records they may inspect, as for assess(). Same-episode
    records cannot be independent corroboration. Different IDs alone establish
    no independence, and this result says nothing about unselected records.
    """
    by_episode = {}; unresolved = []
    for rid in dict.fromkeys(record_ids):
        episode = records.get(rid, {}).get('episode_id')
        if not isinstance(episode, str) or not episode.strip():
            unresolved.append(rid)
        else:
            by_episode.setdefault(episode, []).append(rid)
    repeated = {episode: sorted(ids) for episode, ids in sorted(by_episode.items()) if len(ids) > 1}
    return {'independent': False if repeated else None,
            'independence_basis': 'same_episode' if repeated else 'not_established',
            'episodes': sorted(by_episode), 'same_episode_records': repeated,
            'unresolved_records': sorted(unresolved)}


def impact(records, changed):
    reverse = {}
    for rid, record in records.items():
        dependencies = set(record.get('depends_on', []))
        dependencies.update(child for group in record.get('support_sets', []) for child in group)
        for dependency in dependencies:
            reverse.setdefault(dependency, set()).add(rid)
    queue = [(rid, [rid]) for rid in sorted(changed)]
    visited = set(changed);affected = {}
    while queue:
        parent, path = queue.pop(0)
        for child in sorted(reverse.get(parent, ())):
            if child in visited:
                continue
            visited.add(child)
            affected[child] = {'status': 'needs_review', 'reason': 'ground_or_dependency_changed', 'path': path + [child]}
            queue.append((child, path + [child]))
    return affected
