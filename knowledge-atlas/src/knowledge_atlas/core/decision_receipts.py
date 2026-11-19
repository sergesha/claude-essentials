"""Validate portable canonical decision references independently of service state."""
from datetime import datetime
import json
import re
import subprocess
import uuid
from .git_store import git

DECISIONS = 'refs/knowledge-atlas/decisions/'


def decision_inventory(store, *, decision_id=None):
    selected = uuid.UUID(decision_id) if decision_id is not None else None
    pattern = DECISIONS
    options = []
    if selected is not None:
        # Legacy ref names retain the request's original case, braces and hyphens.
        # A valid UUID has exactly 32 hex digits. Match them in order in Git so
        # unrelated decisions never require object reads or closure validation.
        pattern += '*' + '*'.join(selected.hex) + '*'
        options.append('--ignore-case')
    entries = []
    for line in git(store.root,'for-each-ref',*options,'--format=%(refname) %(objectname)',pattern).splitlines():
        ref, oid = line.split()
        if selected is not None:
            try:
                if uuid.UUID(ref.removeprefix(DECISIONS)) != selected:
                    continue
            except ValueError:
                continue
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


def matching_decision_receipt(store, publication):
    if publication is None:
        return False
    content = publication['content_commit']
    try:
        review = json.loads(git(store.root, 'show', content+':review.json'))
        entries = decision_inventory(store, decision_id=review['decision_id'])
        if len(entries) != 1:
            return False
        receipt = entries[0]['receipt']
        return (receipt['action'] == review['decision'] == 'accept'
                and receipt['accepted_content'] == content
                and receipt['candidate'] == review['candidate_commit']
                and receipt['base_publication'] == review['base_publication'] == publication['previous_publication']
                and receipt['decided_at'] == review['accepted_at'] == publication['accepted_at']
                and receipt['kind'] == publication['acceptance_kind']
                and receipt.get('attribution_channel', 'unrecorded') == review.get('attribution_channel', 'unrecorded')
                and review.get('attribution_channel', 'unrecorded') == publication.get('acceptance_channel', 'unrecorded')
                and all(receipt[key] == review[key] for key in
                        ('decision_id', 'request_sha256', 'actor', 'kind', 'reason')))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def review_inventory(store):
    commits = git(store.root, 'for-each-ref', '--format=%(objectname)', 'refs/poc9/reviews/').splitlines()
    if not commits:
        return
    # One Git process reads the immutable recovery evidence for all old reviews.
    with subprocess.Popen(['git', '-C', str(store.root), 'cat-file', '--batch'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            for commit in commits:
                process.stdin.write((commit + ':review.json\n').encode())
                process.stdin.flush()
                header = process.stdout.readline().split()
                if len(header) != 3 or header[1] != b'blob':
                    raise ValueError('review reference has no review document')
                review = json.loads(process.stdout.read(int(header[2])))
                if process.stdout.read(1) != b'\n':
                    raise ValueError('incomplete review document')
                yield commit, review
        finally:
            process.stdin.close()
        if process.wait():
            raise ValueError('Git cat-file failed to read review documents')
