"""Git-owned review/content/publication history with a single CAS READY ref.

The caller supplies the explicit review decision. Synthetic exercises are stored
as synthetic; this module does not authenticate a human or evaluate truth.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import os
from pathlib import Path
import subprocess
import tempfile
from .git_store import git


def commit_files(repo, base, files, parents, message, *, gitlinks=None, remove_paths=()):
    """Create immutable data objects without changing the working tree/index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'),
                   GIT_AUTHOR_NAME='PoC9 data', GIT_AUTHOR_EMAIL='poc9@example.invalid',
                   GIT_COMMITTER_NAME='PoC9 data', GIT_COMMITTER_EMAIL='poc9@example.invalid')
        def run(*args, data=None):
            result = subprocess.run(['git', '-C', str(repo), *args], input=data,
                                    capture_output=True, env=env)
            if result.returncode:
                raise ValueError(f'Git {args[0]} failed (exit {result.returncode})')
            return result.stdout.decode().strip()
        run('read-tree', base)
        if remove_paths:
            run('update-index', '--force-remove', '--', *remove_paths)
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


class StaleProposal(ValueError):
    """The candidate was prepared against another publication."""


class InvalidAcceptanceTime(ValueError):
    """The decision cannot be placed on the recorded acceptance timeline."""
    code = 'invalid_acceptance_time'


MAX_ACCEPTANCE_CLOCK_SKEW = timedelta(minutes=5)


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError) as error:
        raise InvalidAcceptanceTime('acceptance timeline requires an ISO timestamp with timezone') from error
    if timestamp.tzinfo is None:
        raise InvalidAcceptanceTime('acceptance timeline requires a timezone')
    return timestamp


def acceptance_instant(receipt):
    """Use the immutable publication clock to recover an implausibly future legacy claim."""
    accepted = _timestamp(receipt['accepted_at'])
    published = _timestamp(receipt['published_at'])
    if accepted - published > MAX_ACCEPTANCE_CLOCK_SKEW:
        return published
    return accepted


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
        if 'base_publication' not in decision or self.ready() != decision['base_publication']:
            raise ValueError('stale review: revalidate against current READY')
        self._check_proposal_base(snapshot['manifest'], decision['base_publication'])
        parent = self.read(decision['base_publication']) if decision['base_publication'] else None
        self._check_acceptance_time(decision, self._effective_base_time(decision['base_publication'], parent))
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

    @staticmethod
    def _check_proposal_base(manifest, base):
        if 'proposal_base_publication' not in manifest or manifest['proposal_base_publication'] != base:
            raise StaleProposal('stale proposal: rebase and review against current READY')

    @staticmethod
    def _check_acceptance_time(review, base_time):
        accepted = _timestamp(review.get('accepted_at'))
        if accepted - datetime.now(timezone.utc) > MAX_ACCEPTANCE_CLOCK_SKEW:
            raise InvalidAcceptanceTime('accepted_at is more than five minutes in the future')
        if base_time is not None and accepted < base_time:
            raise InvalidAcceptanceTime('accepted_at precedes the base publication')

    def _effective_base_time(self, oid, parent):
        """Derive the ancestry cutoff, reusing a local result only for its exact Git OID."""
        if parent is None:
            return None
        try:
            cached_oid, cached_value = git(self.store.root, 'config', '--local', '--get', 'atlas.acceptanceCutoff').split()
            cached_time = _timestamp(cached_value)
        except ValueError:
            cached_oid = None
        latest = None
        while parent:
            accepted = acceptance_instant(parent)
            latest = max(latest, accepted) if latest is not None else accepted
            if oid == cached_oid:
                return max(latest, cached_time)
            oid = parent['previous_publication']
            parent = self.read(oid) if oid else None
        return latest

    def ready(self):
        result = subprocess.run(['git', '-C', str(self.store.root), 'show-ref', '--verify', '--hash', self.ref], capture_output=True)
        if result.returncode == 1:
            return None
        if result.returncode:
            # show-ref returns 128 for a missing explicit ref on some Git versions.
            missing = subprocess.run(['git', '-C', str(self.store.root), 'show-ref', '--verify', '--quiet', self.ref])
            if missing.returncode == 1:
                return None
            raise ValueError(f'Git show-ref failed (exit {result.returncode})')
        return result.stdout.decode().strip()

    def read(self, publication):
        snapshot = self.store.read(publication)
        return self._read_receipt(publication, snapshot['manifest'])

    def verify_child_receipt(self, publication, content_commit):
        """Check an edge without recursively loading the same package closure again."""
        if not isinstance(publication, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', publication):
            raise ValueError('exact child publication required')
        wrapper = json.loads(git(self.store.root, 'show', publication + ':manifest.json'))
        if self._read_receipt(publication, wrapper)['content_commit'] != content_commit:
            raise ValueError('child publication/content mismatch')

    def _read_receipt(self, publication, wrapper_manifest):
        receipt = json.loads(git(self.store.root, 'show', publication + ':publication.json'))
        parents = git(self.store.root, 'show', '-s', '--format=%P', publication).split()
        content = receipt['content_commit']
        previous = receipt['previous_publication']
        if parents != ([previous, content] if previous else [content]):
            raise ValueError('publication does not reference its accepted content and previous publication')
        changed = git(self.store.root, 'diff-tree', '-r', '--no-renames', '--ignore-submodules=none',
                      '--name-only', content, publication)
        if changed != 'publication.json':
            raise ValueError('publication tree differs from reviewed content')
        manifest = json.loads(git(self.store.root, 'show', content + ':manifest.json'))
        if wrapper_manifest != manifest:
            raise ValueError('publication requires accepted content')
        review = self._accepted_review(content, manifest)
        if (review['base_publication'] != previous
                or review['kind'] != receipt['acceptance_kind']
                or ('acceptance_channel' in receipt and
                    receipt['acceptance_channel'] != review.get('attribution_channel', 'unrecorded'))
                or review['accepted_at'] != receipt['accepted_at']
                or receipt['profile_id'] != manifest['profile_id']):
            raise ValueError('publication metadata disagrees with accepted content')
        receipt.setdefault('acceptance_channel', 'unrecorded')
        return receipt

    def _accepted_review(self, content, manifest):
        if manifest.get('status') != 'accepted':
            raise ValueError('publication requires accepted content')
        review = json.loads(git(self.store.root, 'show', content + ':review.json'))
        if (not isinstance(review, dict) or review.get('decision') != 'accept'
                or manifest.get('acceptance') != review or review.get('accepted_at') != manifest.get('accepted_at')):
            raise ValueError('accepted_at or review metadata disagrees with accepted content')
        if git(self.store.root, 'show', '-s', '--format=%P', content).split() != [review.get('candidate_commit')]:
            raise ValueError('accepted content does not descend directly from the reviewed candidate')
        return review

    def publish(self, content, build_projection):
        snapshot = self.store.read(content)
        if snapshot['manifest']['status'] != 'accepted':
            raise ValueError('unreviewed candidate cannot be published')
        review = self._accepted_review(content, snapshot['manifest'])
        previous = self.ready()
        parent = self.read(previous) if previous else None
        if parent and parent['content_commit'] == content:
            return previous  # Reconcile a successful CAS whose acknowledgement was lost.
        if review['base_publication'] != previous:
            raise ValueError('stale review: revalidate against current READY')
        self._check_proposal_base(snapshot['manifest'], previous)
        base_time = self._effective_base_time(previous, parent)
        self._check_acceptance_time(review, base_time)
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
                       'acceptance_channel': review.get('attribution_channel', 'unrecorded'),
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
        cutoff = acceptance_instant(publication)
        if base_time is not None:
            cutoff = max(cutoff, base_time)
        # This optional cache belongs to the local repository. Portable archives
        # carry the immutable evidence used to reconstruct it after restore.
        try:
            git(self.store.root, 'config', '--local', 'atlas.acceptanceCutoff', result + ' ' + cutoff.isoformat())
        except ValueError:
            pass  # READY has already advanced. A cache miss only costs a history walk.
        return result
