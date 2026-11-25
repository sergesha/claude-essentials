"""Verify the identity and authenticated Atlas status of an ARM64 release image."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


REGISTRY = "ghcr.io/sergesha/knowledge-atlas"
SOURCE = "https://github.com/sergesha/claude-essentials"
PLATFORM = "linux/arm64"
DIGEST = r"sha256:[0-9a-f]{64}"
PYTHON = "/opt/knowledge-atlas/.venv/bin/python"

# The packaged service starts in an isolated network with new local credentials.
# Full supervisor, model and native pipeline qualification runs separately.
HANDSHAKE = """
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from knowledge_atlas.client import AtlasClient
from knowledge_atlas.contracts import AtlasError

with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    service = directory / "service"
    config = directory / "config.json"
    config.write_text(json.dumps({"service_root": str(service)}))
    with subprocess.Popen(
        [sys.executable, "-m", "knowledge_atlas.cli", "serve", "--config", str(config)],
        stdout=sys.stderr,
    ) as process:
        try:
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    version = AtlasClient(service).verify_ready()
                    print(json.dumps({"application_version": version}))
                    break
                except AtlasError as error:
                    if error.code != "service_unavailable":
                        raise
                    time.sleep(0.2)
            else:
                raise SystemExit("Packaged Atlas application handshake failed")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
"""


def docker(config, *args, timeout=90):
    command = ["docker", "--config", str(config), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout).stdout


def verify(image, version, revision, *, local=False):
    expected_ref = DIGEST if local else re.escape(REGISTRY) + "@" + DIGEST
    if not re.fullmatch(expected_ref, image):
        raise ValueError("expected an immutable Atlas image digest")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("expected the exact source revision")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("expected the coordinated application version")

    with tempfile.TemporaryDirectory(prefix="atlas-anonymous-docker-") as directory:
        config = Path(directory)
        # An explicit empty entry also prevents native credential-store discovery.
        (config / "config.json").write_text(json.dumps({"auths": {"ghcr.io": {}}}))
        if not local:
            docker(config, "pull", "--platform", PLATFORM, image, timeout=300)
        metadata, = json.loads(docker(config, "image", "inspect", image))
        config_id = metadata["Id"]
        if not re.fullmatch(DIGEST, config_id) or (local and image != config_id):
            raise ValueError("image config digest mismatch")
        if not local and image not in (metadata.get("RepoDigests") or ()):
            raise ValueError("registry image digest mismatch")
        if f"{metadata['Os']}/{metadata['Architecture']}" != PLATFORM:
            raise ValueError("image platform mismatch")
        labels = metadata["Config"]["Labels"]
        for key, value in {"source": SOURCE, "version": version, "revision": revision}.items():
            if labels.get(f"org.opencontainers.image.{key}") != value:
                raise ValueError(f"OCI {key} label mismatch")
        handshake = json.loads(docker(
            config, "run", "--rm", "--pull=never", "--platform", PLATFORM,
            "--network", "none", "--entrypoint", PYTHON, image, "-c", HANDSHAKE,
        ))
        if handshake.get("application_version") != version:
            raise ValueError("authenticated application version mismatch")
        return {
            "image": image, "config_id": config_id, "version": version,
            "revision": revision, "platform": PLATFORM,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--image", help="Anonymous GHCR reference pinned by its manifest digest")
    selection.add_argument("--local-id", help="Local scanned candidate selected by its config ID")
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.image or args.local_id, args.version, args.revision, local=args.local_id is not None)
    except subprocess.CalledProcessError as error:
        print(f"Atlas image verification failed: {(error.stderr or str(error)).strip()}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Atlas image verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
