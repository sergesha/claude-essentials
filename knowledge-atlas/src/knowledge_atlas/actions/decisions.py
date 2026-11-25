"""Immutable decision receipts around PoC9's separate review/publication steps."""
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json

from ..contracts import ATTRIBUTION_CHANNELS, ActionRequest, ActionResult, AtlasError
from ..project_registry import project_store
from ..core.git_store import git
from ..core.publication import Publications, StaleProposal, InvalidAcceptanceTime, commit_files
from ..core.knowledge import propose_records
from ..core.decision_receipts import DECISIONS, review_inventory
from ..runtime import _assets



def _request_digest(request):
    body = asdict(request)
    return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _binding(context, action):
    request, data = context.request, context.request.payload
    if request.request_id is None:
        raise AtlasError('invalid_request', 'decision requires a request UUID')
    for key in ('candidate','actor','reason','decided_at'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise AtlasError('invalid_request', key+' is required')
    ActionRequest('candidate.inspect', publication=data['candidate'])
    if action not in ('accept','reject','revise') or data.get('kind') not in ('human','synthetic'):
        raise AtlasError('invalid_request', 'explicit decision action and human/synthetic kind required')
    try:
        if datetime.fromisoformat(data['decided_at'].replace('Z','+00:00')).tzinfo is None: raise ValueError()
    except ValueError as error:
        raise AtlasError('invalid_request','decided_at requires a timezone') from error
    channel = context.configuration.get('attribution_channel')
    if channel not in ATTRIBUTION_CHANNELS:
        raise AtlasError('invalid_request', 'recorded decision attribution channel required')
    digest = _request_digest(request)
    return {'schema_version':1,'decision_id':request.request_id,'request_sha256':digest,
            'candidate':data['candidate'],'base_publication':request.expected_publication,'action':action,
            'attribution_channel':channel,
            **({'operation_id': context.operation_id} if context.operation_id is not None else {}),
            **{key:data[key] for key in ('actor','kind','reason','decided_at')}}


def decision_inventory(store, *, decision_id=None):
    from ..core.decision_receipts import decision_inventory as canonical_inventory
    try:
        return canonical_inventory(store, decision_id=decision_id)
    except ValueError as error:
        raise AtlasError('invalid_decision',str(error),422) from error


def _check_replay(binding, recorded):
    if any(recorded.get(key) != value for key, value in binding.items() if key not in ('attribution_channel', 'operation_id')):
        raise AtlasError('request_conflict','canonical decision ID already has another basis',409)


def _existing(store, binding):
    matches = decision_inventory(store, decision_id=binding['decision_id'])
    if not matches: return None
    entry = matches[0]
    _check_replay(binding, entry['receipt'])
    return entry


def _write_receipt(store, binding, **effects):
    previous = _existing(store, binding)
    if previous is not None: return previous
    receipt={**binding,**effects}
    parents=list(dict.fromkeys(oid for oid in (binding['candidate'],binding['base_publication'],
        effects.get('accepted_content'),effects.get('revised_candidate')) if oid))
    for oid in parents: store.read(oid)
    oid=commit_files(store.root,binding['candidate'],{'atlas-decision.json':receipt},parents,'Record explicit '+binding['action']+' decision')
    store.read(oid)
    ref=DECISIONS+binding['decision_id']
    git(store.root,'update-ref',ref,oid,'0'*len(oid))
    return {'ref':ref,'commit':oid,'receipt':receipt}


def _published_content(pubs, content):
    selected=pubs.ready(); seen=set()
    while selected and selected not in seen:
        seen.add(selected)
        receipt=pubs.read(selected)
        if receipt['content_commit']==content: return selected
        selected=receipt['previous_publication']
    return None


def build_projection(context, content):
    from ..native_pipeline import run_native
    request=replace(context.request,action='publication.build',payload={'content':content})
    return run_native(replace(context,request=request)).data


def _require_original_operation(context, record):
    if (record.get('operation_id') != context.operation_id
            or record['attribution_channel'] != context.configuration.get('attribution_channel')):
        raise AtlasError('unsafe_resume', 'continuation requires the original acceptance operation and channel', 409)


def _result(context, entry, *, publish=True):
    store=project_store(context.service, context.atlas_path); pubs=Publications(store); receipt=entry['receipt']
    data={'decision':receipt,'decision_commit':entry['commit'],'candidate':receipt.get('revised_candidate',receipt['candidate'])}
    publication=None
    effects={'decision':entry['commit']}
    if receipt['action']=='accept':
        content=receipt['accepted_content']; data['accepted_content']=content; effects['accepted_content']=content
        if pubs.ready() != receipt['base_publication']:
            publication=_published_content(pubs,content)
        if publication is None:
            if not publish:
                raise AtlasError('unsafe_resume', 'explicit resume of the original acceptance is required', 409)
            _require_original_operation(context, receipt)
        try:
            if publication is None: publication=pubs.publish(content,lambda oid:build_projection(context,oid))
            data['status']='published'; effects['publication']=publication
        except Exception as error:
            data.update(status='accepted_pending_publication',publication_error={
                'code':getattr(error,'code','projection_failed'),'message':str(error)},current_ready=pubs.ready())
    else:
        data['status']='rejected' if receipt['action']=='reject' else 'pending_review'
    return ActionResult(data=data,publication=publication,evidence=[str(context.evidence)],canonical_effects=effects)


def _check_base(store, binding):
    if Publications(store).ready()!=binding['base_publication']:
        raise AtlasError('stale_publication','READY changed; review against the current publication',409)
    snapshot=store.read(binding['candidate'])
    if snapshot['manifest']['status']!='candidate':
        raise AtlasError('invalid_candidate','decision requires an unaccepted candidate',422)
    return snapshot


def decide_candidate(context):
    if context.request.payload.get('action') not in ('accept', 'reject'):
        raise AtlasError('invalid_request', 'candidate.decide accepts only accept or reject')
    binding=_binding(context,context.request.payload.get('action'))
    store=project_store(context.service, context.atlas_path)
    existing=_existing(store,binding)
    if existing is not None: return _result(context,existing,publish=False)
    _check_base(store,binding)
    effects={}
    if binding['action']=='accept':
        if _accepted_effect(store, binding) is not None:
            raise AtlasError('unsafe_resume', 'explicit resume of the original acceptance is required', 409)
        decision={**binding,'decision':'accept','candidate_commit':binding['candidate'],'accepted_at':binding['decided_at']}
        profile=json.loads((_assets()/'embedding-profile.json').read_text())
        try:
            content=Publications(store).review(binding['candidate'],decision,profile)
        except StaleProposal as error:
            raise AtlasError('stale_proposal', str(error), 409) from error
        except InvalidAcceptanceTime as error:
            raise AtlasError('invalid_acceptance_time', str(error), 422) from error
        effects['accepted_content']=content
    return _result(context,_write_receipt(store,binding,**effects))


def _accepted_effect(store, binding):
    matches = []
    for oid, review in review_inventory(store):
        if review['decision_id'] != binding['decision_id']:
            continue
        _check_replay(binding, review)
        manifest = store.read(oid)['manifest']
        Publications(store)._accepted_review(oid, manifest)
        original = {key: review[key] for key in binding if key != 'operation_id'}
        if 'operation_id' in review:
            original['operation_id'] = review['operation_id']
        matches.append((oid, original))
    if len(matches) > 1:
        raise AtlasError('unsafe_resume', 'multiple accepted effects require inspection', 409)
    return matches[0] if matches else None


def replay_recorded_decision(context):
    action = 'revise' if context.request.action == 'candidate.revise' else context.request.payload.get('action')
    binding = _binding(context, action)
    entry = _existing(project_store(context.service, context.atlas_path), binding)
    return _result(context, entry, publish=False) if entry is not None else None


def reconcile_decision(context):
    action='revise' if context.request.action=='candidate.revise' else context.request.payload.get('action')
    binding=_binding(context,action); store=project_store(context.service, context.atlas_path)
    entry=_existing(store,binding)
    if entry is not None: return _result(context,entry)
    if action=='accept':
        recovered = _accepted_effect(store, binding)
        if recovered is not None:
            accepted, original = recovered
            _require_original_operation(context, original)
            _check_base(store, binding)
            return _result(context, _write_receipt(store, original, accepted_content=accepted))
    if action in ('accept', 'revise'):
        raise AtlasError('unsafe_resume','no canonical effect establishes safe continuation',409)
    return None


def revise_candidate(context):
    binding=_binding(context,'revise'); store=project_store(context.service, context.atlas_path)
    existing=_existing(store,binding)
    if existing is not None: return _result(context,existing,publish=False)
    _check_base(store,binding)
    data=context.request.payload
    if 'entries' in data:
        if not isinstance(data['entries'],list): raise AtlasError('invalid_request','entries must be an array')
        revised=propose_records(store,binding['candidate'],data['entries'],actor=binding['actor'],
            method='Explicit candidate revision',reason=binding['reason'],recorded_at=binding['decided_at'],
            base_publication=binding['base_publication'])
    else:
        from ..native_pipeline import run_native
        revised=run_native(context).data['candidate']
    return _result(context,_write_receipt(store,binding,revised_candidate=revised))


def _json_snapshot(value):
    if isinstance(value, bytes): return value.decode('utf-8')
    if isinstance(value, dict): return {key: _json_snapshot(item) for key, item in value.items()}
    if isinstance(value, list): return [_json_snapshot(item) for item in value]
    return value


def inspect_candidate(context):
    store=project_store(context.service, context.atlas_path); candidate=context.request.payload.get('candidate')
    ActionRequest('candidate.inspect',publication=candidate)
    if candidate is None: raise AtlasError('invalid_request','candidate is required')
    snapshot=store.read(candidate)
    base=snapshot['manifest'].get('proposal_base_content')
    before = store.read(base) if base else None
    from ..core.composition import composition_diff
    changes = composition_diff(before['manifest']['modules'].values() if before else (), snapshot['manifest']['modules'].values())
    return ActionResult(data={'candidate':candidate,'snapshot':_json_snapshot(snapshot),'base_content':base,
        'before':_json_snapshot(before), 'composition_diff': changes,
        'decisions':[entry['receipt'] for entry in decision_inventory(store) if entry['receipt']['candidate']==candidate],
        'type_questions':snapshot.get('relations',{}).get('decisions',[]),
        'unresolved':[row for row in snapshot.get('relations',{}).get('relations',[]) if row['status']!='resolved']},
        publication=Publications(store).ready())
