"""Validate portable canonical decision references independently of service state."""
from datetime import datetime
import json
import re
import uuid
from .git_store import git

DECISIONS = 'refs/knowledge-atlas/decisions/'


def decision_inventory(store):
    entries = []
    for line in git(store.root,'for-each-ref','--format=%(refname) %(objectname)',DECISIONS).splitlines():
        ref, oid = line.split()
        try:
            store.read(oid)
            receipt = json.loads(git(store.root,'show',oid+':atlas-decision.json'))
            uuid.UUID(receipt['decision_id'])
            if receipt['schema_version'] != 1 or ref != DECISIONS+receipt['decision_id']:
                raise ValueError('ref and receipt disagree')
            if receipt['action'] not in ('accept','reject','revise') or receipt['kind'] not in ('human','synthetic'):
                raise ValueError('invalid action or attribution')
            if any(not isinstance(receipt.get(key),str) or not receipt[key].strip() for key in ('actor','reason','decided_at')):
                raise ValueError('missing attribution')
            if datetime.fromisoformat(receipt['decided_at'].replace('Z','+00:00')).tzinfo is None:
                raise ValueError('decision timezone required')
            if not re.fullmatch(r'[0-9a-f]{64}',receipt['request_sha256']): raise ValueError('request digest required')
            required = ['candidate','base_publication']
            if receipt['action'] == 'accept': required.append('accepted_content')
            if receipt['action'] == 'revise': required.append('revised_candidate')
            for key in required:
                referenced = receipt[key]
                if key == 'base_publication' and referenced is None: continue
                if not isinstance(referenced,str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',referenced):
                    raise ValueError('exact referenced commit required')
                store.read(referenced)
                git(store.root,'merge-base','--is-ancestor',referenced,oid)
            entries.append({'ref':ref,'commit':oid,'receipt':receipt})
        except (ValueError,KeyError,TypeError,AttributeError,OSError) as error:
            raise ValueError('invalid decision '+ref+': '+str(error)) from error
    return entries
