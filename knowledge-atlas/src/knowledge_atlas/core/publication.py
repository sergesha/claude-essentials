"""Git-owned review/content/publication history with a single CAS READY ref.

The caller supplies the explicit review decision. Synthetic exercises are stored
as synthetic; this module does not authenticate a human or evaluate truth.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from .git_store import git


def commit_files(repo, base, files, parents, message, *, gitlinks=None):
    """Create immutable data objects without changing the working tree/index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'),
                   GIT_AUTHOR_NAME='PoC9 data', GIT_AUTHOR_EMAIL='poc9@example.invalid',
                   GIT_COMMITTER_NAME='PoC9 data', GIT_COMMITTER_EMAIL='poc9@example.invalid')
        def run(*args, data=None):
            result = subprocess.run(['git', '-C', str(repo), *args], input=data,
                                    capture_output=True, env=env)
            if result.returncode:
                raise ValueError(result.stderr.decode(errors='replace'))
            return result.stdout.decode().strip()
        run('read-tree', base)
        for name, value in files.items():
            raw = value if isinstance(value,bytes) else (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
            oid = run('hash-object', '-w', '--stdin', data=raw)
            run('update-index', '--add', '--cacheinfo', '100644,' + oid + ',' + name)
        for name,oid in (gitlinks or {}).items():
            run('update-index','--add','--cacheinfo','160000,'+oid+','+name)
        tree = run('write-tree')
        args = ['commit-tree', tree]
        for parent in parents:
            args += ['-p', parent]
        return run(*args, '-m', message)


class Publications:
    ref = 'refs/heads/ready'

    def __init__(self, store):
        self.store = store

    def review(self, candidate, decision, profile):
        snapshot = self.store.read(candidate)
        if snapshot['manifest']['status'] != 'candidate':
            raise ValueError('review requires a candidate')
        if 'candidate_commit' in decision and decision['candidate_commit'] != candidate:
            raise ValueError('new content requires review: decision belongs to another candidate')
        if decision.get('decision') != 'accept' or decision.get('kind') not in ('human', 'synthetic'):
            raise ValueError('explicit human or synthetic acceptance decision required')
        if not decision.get('actor') or not decision.get('reason'):
            raise ValueError('review actor and reason required')
        accepted = datetime.fromisoformat(decision['accepted_at'].replace('Z', '+00:00'))
        if accepted.tzinfo is None:
            raise ValueError('accepted_at requires timezone')
        if 'base_publication' not in decision or self.ready() != decision['base_publication']:
            raise ValueError('stale review: revalidate against current READY')
        if ('proposal_base_publication' in snapshot['manifest']
                and snapshot['manifest']['proposal_base_publication'] != decision['base_publication']):
            raise ValueError('stale proposal: rebase and review against current READY')
        digest = hashlib.sha256(json.dumps(profile['profile'], sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if digest != profile['profile_id']:
            raise ValueError('profile digest mismatch')
        review = {**decision, 'candidate_commit': candidate}
        manifest = {**snapshot['manifest'], 'status': 'accepted',
                    'accepted_at': decision['accepted_at'], 'acceptance': review,
                    'profile_id': profile['profile_id']}
        content = commit_files(self.store.root, candidate,
            {'manifest.json': manifest, 'review.json': review, 'embedding-profile.json': profile},
            [candidate], 'Record explicit review decision and accepted content')
        # Keep pending reviewed content reachable even before projections finish.
        git(self.store.root, 'update-ref', 'refs/poc9/reviews/' + content, content)
        return content

    def ready(self):
        result = subprocess.run(['git', '-C', str(self.store.root), 'show-ref', '--verify', '--hash', self.ref], capture_output=True)
        if result.returncode == 1:
            return None
        if result.returncode:
            # show-ref returns 128 for a missing explicit ref on some Git versions.
            missing = subprocess.run(['git', '-C', str(self.store.root), 'show-ref', '--verify', '--quiet', self.ref])
            if missing.returncode == 1:
                return None
            raise ValueError(result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def read(self, publication):
        self.store.read(publication)  # Exact OID and package closure validation.
        return json.loads(git(self.store.root, 'show', publication + ':publication.json'))

    def publish(self, content, build_projection):
        snapshot = self.store.read(content)
        if snapshot['manifest']['status'] != 'accepted':
            raise ValueError('unreviewed candidate cannot be published')
        review = json.loads(git(self.store.root, 'show', content + ':review.json'))
        previous = self.ready()
        if previous and self.read(previous)['content_commit'] == content:
            return previous  # Reconcile a successful CAS whose acknowledgement was lost.
        if review['base_publication'] != previous:
            raise ValueError('stale review: revalidate against current READY')
        receipt = build_projection(content)
        if receipt.get('profile_id') != snapshot['manifest']['profile_id']:
            raise ValueError('projection profile does not match accepted content')
        if (receipt.get('content_commit') != content or receipt.get('ready') is not True
                or not receipt.get('graph_commit')
                or receipt.get('search_checkpoint') != receipt['graph_commit']):
            raise ValueError('projection is not ready for this exact content')
        publication = {**receipt, 'previous_publication': previous,
                       'profile_id': snapshot['manifest']['profile_id'],
                       'acceptance_kind': review['kind'], 'accepted_at': review['accepted_at'],
                       'published_at': datetime.now().astimezone().isoformat()}
        parents = [previous, content] if previous else [content]
        result = commit_files(self.store.root, content, {'publication.json': publication}, parents,
                              'Publish consistent accepted atlas slice')
        try:
            git(self.store.root, 'update-ref', self.ref, result, previous or '0' * len(content))
        except ValueError as error:
            # A competing publisher may have won while this projection was built.
            current = self.ready()
            if current and self.read(current)['content_commit'] == content:
                return current
            raise ValueError('stale publication: READY changed; semantic revalidation required') from error
        return result
