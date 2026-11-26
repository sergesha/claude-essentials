#!/usr/bin/env python3
"""Check anonymous delivery of the exact published multiarchitecture images."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from publish_images import COMPONENTS, check_index, manifest


def verify(receipts, version, revision, arch):
    matching = [json.loads(p.read_text()) for p in receipts.rglob('redis-publication.json')]
    matching = [r for r in matching if r['version'] == version and r['revision'] == revision
                and set(r['images']) == set(COMPONENTS)]
    if not matching or any(r != matching[0] for r in matching):
        raise ValueError('No unique complete release receipt')
    for component, image in matching[0]['images'].items():
        repo = COMPONENTS[component]
        assert image['repository'] == repo
        found = manifest(f'{repo}:{version}')
        assert found and found[1] == image['digest'], 'Version tag differs from receipt'
        check_index(found[0], {a: p['digest'] for a, p in image['platforms'].items()})
        ref = f"{repo}@{image['digest']}"
        subprocess.run(['docker', 'pull', '--platform', f'linux/{arch}', ref], check=True)
        config = json.loads(subprocess.check_output(['docker', 'image', 'inspect', ref]))[0]
        assert config['Id'] == image['platforms'][arch]['config_id']
        labels = config['Config']['Labels']
        assert labels['org.opencontainers.image.version'] == version
        assert labels['org.opencontainers.image.revision'] == revision
        assert labels['org.opencontainers.image.source'] == 'https://github.com/sergesha/claude-essentials'
        if component == 'mcp':
            subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'tests/smoke_mcp.py'), ref], check=True)
        else:
            subprocess.run(['docker', 'run', '--rm', ref, 'python', '-c',
                            'import onnxruntime, embedding_service; print("Embedding runtime imports OK")'], check=True)
        print(json.dumps({'component': component, 'platform': f'linux/{arch}', 'image': ref, 'verified': True}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--arch', choices=['amd64','arm64'], required=True)
    args = parser.parse_args()
    verify(args.receipts, args.version, args.revision, args.arch)
