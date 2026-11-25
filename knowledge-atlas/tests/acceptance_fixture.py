"""Record the service decision for an accepted candidate without building a projection."""
from uuid import uuid4

from knowledge_atlas.actions.decisions import _binding, _write_receipt
from knowledge_atlas.contracts import ActionContext, ActionRequest, ServiceContext


def review_acceptance(pubs, candidate, decision, profile):
    store = pubs.store
    service = ServiceContext(store.base.parent / 'fixture-service')
    request = ActionRequest('candidate.decide', atlas_id=store.read(candidate)['manifest']['package_id'],
                            request_id=str(uuid4()), expected_publication=decision['base_publication'],
                            payload={'action': 'accept', 'candidate': candidate, 'decided_at': decision['accepted_at'],
                                     **{key: decision[key] for key in ('actor', 'kind', 'reason')}})
    context = ActionContext(service, request, store.base, service.service_root,
                            {'attribution_channel': decision.get('attribution_channel', 'python')})
    binding = _binding(context, 'accept')
    review = {**binding, 'decision': 'accept', 'candidate_commit': candidate, 'accepted_at': decision['accepted_at']}
    content = pubs.review(candidate, review, profile)
    _write_receipt(store, binding, accepted_content=content)
    return content
