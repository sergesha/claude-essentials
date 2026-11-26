#!/usr/bin/env python3
"""Publish only the four exact candidates approved by the shared image gate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = {'mcp': 'ghcr.io/sergesha/redis-memory-mcp',
              'embeddings': 'ghcr.io/sergesha/redis-memory-embeddings'}
ARCHES = ('amd64', 'arm64')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')

def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE).stdout

def manifest(ref):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', ref, '--raw'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Authentication, rate limits and transport errors are not absence.
        if result.stderr.decode().strip() == f'ERROR: {ref}: not found':
            return None
        raise RuntimeError('Registry lookup failed: ' + result.stderr.decode())
    return json.loads(result.stdout), 'sha256:' + hashlib.sha256(result.stdout).hexdigest()

def check_single(document, config_id):
    if document.get('config', {}).get('digest') != config_id or 'manifests' in document:
        raise ValueError('Existing platform version has different image bytes')

def check_index(document, expected):
    entries = document.get('manifests', [])
    actual = {(m.get('platform', {}).get('os'), m.get('platform', {}).get('architecture')):
              m.get('digest') for m in entries}
    if len(entries) != 2 or actual != {('linux', arch): digest for arch, digest in expected.items()}:
        raise ValueError('Version index differs from the two verified platforms')

def publish(directory, version, revision, output):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Invalid release version/revision')
    candidates = {}
    # Validate every archive and decision before making any registry mutation.
    for component in COMPONENTS:
        for arch in ARCHES:
            evidence = (directory / f'redis-candidate-{component}-{arch}').resolve()
            candidate = json.loads((evidence / 'reports/candidate.json').read_text())
            if (candidate['version'] != version or candidate['revision'] != revision
                    or candidate['platform'] != f'linux/{arch}'):
                raise ValueError('Candidate release identity mismatch')
            run('docker', 'load', '-i', str(evidence / 'candidate.tar'))
            run(sys.executable, str(ROOT / 'tools/image_security/gate.py'), 'verify',
                '--image', candidate['id'], '--platform', f'linux/{arch}',
                '--revision', revision, '--out', str(evidence))
            candidates[component, arch] = candidate
    # Preflight all existing tags. A rerun may reuse identical bytes only.
    existing = {}
    indexes = {}
    for component, repo in COMPONENTS.items():
        indexes[component] = manifest(f'{repo}:{version}')
        for arch in ARCHES:
            found = manifest(f'{repo}:{version}-{arch}')
            if found:
                check_single(found[0], candidates[component, arch]['id'])
            existing[component, arch] = found
        if indexes[component]:
            if not all(existing[component, a] for a in ARCHES):
                raise ValueError('Existing version index has no matching platform tags')
            check_index(indexes[component][0], {a: existing[component, a][1] for a in ARCHES})
    result = {'version': version, 'revision': revision, 'images': {}}
    for component, repo in COMPONENTS.items():
        children = {}
        for arch in ARCHES:
            tag = f'{repo}:{version}-{arch}'
            candidate = candidates[component, arch]
            found = existing[component, arch]
            if not found:
                # Recheck immediately before publication, including evidence freshness.
                if manifest(tag) is not None:
                    raise ValueError('Platform tag appeared during publication')
                evidence = (directory / f'redis-candidate-{component}-{arch}').resolve()
                run(sys.executable, str(ROOT / 'tools/image_security/gate.py'), 'verify',
                    '--image', candidate['id'], '--platform', f'linux/{arch}',
                    '--revision', revision, '--out', str(evidence))
                run('docker', 'tag', candidate['id'], tag)
                pushed = run('docker', 'push', tag).decode()
                match = re.search(r'digest: (sha256:[a-f0-9]{64}) size:', pushed)
                if not match:
                    raise ValueError('Docker push did not return a manifest digest')
                digest = match[1]
                found = manifest(f'{repo}@{digest}')
                if not found or found[1] != digest:
                    raise ValueError('Published platform digest mismatch')
                check_single(found[0], candidate['id'])
                if manifest(tag) != found:
                    raise ValueError('Platform tag changed after publication')
            children[arch] = found[1]
        tag = f'{repo}:{version}'
        found = indexes[component]
        if not found:
            if manifest(tag) is not None:
                raise ValueError('Version tag appeared during publication')
            run('docker', 'buildx', 'imagetools', 'create', '--tag', tag,
                *[f'{repo}@{children[a]}' for a in ARCHES])
            found = manifest(tag)
        if not found:
            raise ValueError('Published version index missing')
        check_index(found[0], children)
        result['images'][component] = {'repository': repo, 'digest': found[1], 'platforms': {
            arch: {'digest': children[arch], 'config_id': candidates[component, arch]['id']}
            for arch in ARCHES}}
        output.write_text(json.dumps(result, indent=2) + '\n')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    publish(args.candidates, args.version, args.revision, args.output)
