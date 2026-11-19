"""Runtime launch must not consume an unused atlas/restore destination."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def test_bridge_start_preserves_unused_atlas_destination(tmp_path):
    package=Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    destination=tmp_path/'unused-atlas'
    process=subprocess.Popen([sys.executable,str(package/'runtime/embedding-provider.py'),
        '--profile',str(package/'runtime/embedding-profile.json'),'--cache',str(destination/'vectors'),
        '--endpoint','http://127.0.0.1:1','--port',str(port)],
        env={**os.environ,'PYTHONPATH':str(package/'src')},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while True:
            assert process.poll() is None
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/stats',timeout=1) as response:
                    assert json.load(response)['inference_calls']==0
                break
            except OSError:
                assert time.monotonic()<deadline
                time.sleep(.05)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/models',timeout=1) as response:
            assert json.load(response)['data'][0]['id'].startswith('poc9-gemma-q4-')
        assert not destination.exists(), 'launch/readiness must leave create/restore destination unused'
    finally:
        process.terminate();process.wait(timeout=10)
