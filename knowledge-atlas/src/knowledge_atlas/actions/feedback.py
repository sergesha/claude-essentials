"""Feedback and explicit revisions remain PoC9 proposals until knowledge review."""
from dataclasses import replace
from datetime import datetime
import json
from ..contracts import ActionRequest, ActionResult, AtlasError
from ..project_registry import project_store
from ..core.git_store import git
from ..core.publication import Publications
from ..core.feedback import record_case_outcome
from ..core.knowledge import propose_records


def feedback_action(context):
    data = context.request.payload
    for field in ('actor','recorded_at','record_id'):
        if not isinstance(data.get(field),str) or not data[field].strip():
            raise AtlasError('invalid_request',field+' is required')
    try:
        if datetime.fromisoformat(data['recorded_at'].replace('Z','+00:00')).tzinfo is None: raise ValueError()
    except ValueError as error:
        raise AtlasError('invalid_request','recorded_at requires a timezone') from error
    store = project_store(context.service, context.atlas_path); pubs = Publications(store)
    expected = context.request.expected_publication
    if expected is None or pubs.ready() != expected:
        raise AtlasError('stale_publication','select current READY before proposing feedback',409)
    content = pubs.read(expected)['content_commit']
    try:
        if context.request.action == 'feedback.record':
            applied = data.get('applied_publication')
            if applied is None: raise AtlasError('invalid_request','applied_publication is required')
            ActionRequest('feedback.record',publication=applied)
            current = expected
            while current and current != applied: current = pubs.read(current)['previous_publication']
            if current is None: raise AtlasError('invalid_request','applied publication is not in retained published history')
            raw = data.get('raw_report')
            if not isinstance(raw,str): raise AtlasError('invalid_request','raw_report must retain the submitted JSON text')
            candidate = record_case_outcome(store,content,data['record_id'],raw,actor=data['actor'],
                recorded_at=data['recorded_at'],base_publication=expected,application_publication=applied)
        elif context.request.action == 'knowledge.revise':
            from ..core.graph_stage import records_for
            if data['record_id'] not in {row['@id'] for row in records_for(store.read(content),content)}:
                raise AtlasError('record_not_found','revision target is absent from selected current content',404)
            if 'entries' in data and any(key in data for key in ('sources','remove_source_ids','instructions')):
                raise AtlasError('invalid_request','choose structured entries or native correction')
            if not isinstance(data.get('reason'),str) or not data['reason'].strip():
                raise AtlasError('invalid_request','revision reason required')
            if 'entries' in data:
                if not isinstance(data['entries'],list): raise AtlasError('invalid_request','entries must be an array')
                candidate = propose_records(store,content,data['entries'],actor=data['actor'],method='Explicit knowledge revision',
                    reason=data['reason'],recorded_at=data['recorded_at'],base_publication=expected)
            else:
                if data.get('kind') not in ('human','synthetic'):
                    raise AtlasError('invalid_request','native correction requires explicit human/synthetic attribution')
                if not data.get('sources') and not data.get('remove_source_ids') and not data.get('instructions'):
                    raise AtlasError('invalid_request','revision requires entries or a native source correction')
                seed = propose_records(store,content,[],actor=data['actor'],method='Native knowledge correction',
                    reason=data['reason'],recorded_at=data['recorded_at'],base_publication=expected)
                from ..native_pipeline import run_native
                native_request = replace(context.request,action='candidate.revise',payload={**data,
                    'candidate':seed,'decided_at':data['recorded_at']})
                candidate = run_native(replace(context,request=native_request)).data['candidate']
        else:
            raise AtlasError('unsupported_action',context.request.action,422)
    except (ValueError,KeyError,TypeError) as error:
        raise AtlasError('invalid_request',str(error)) from error
    proposal = json.loads(git(store.root,'show',candidate+':proposal.json'))
    return ActionResult(data={'candidate':candidate,'status':'pending_review',
        'impact':proposal.get('impact',{}),'wiki_impact':proposal.get('wiki_impact',{}),
        'revision_targets':proposal.get('revision_targets',[data['record_id']])},publication=expected,
        evidence=[str(context.evidence)],canonical_effects={'candidate':candidate})
