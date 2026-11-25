"""Release workflow contracts and isolated registry verification failures."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


PACKAGE = Path(__file__).resolve().parents[1]
WORKFLOWS = PACKAGE.parent / ".github" / "workflows"
REGISTRY = "ghcr.io/sergesha/knowledge-atlas"
REVISION = "a" * 40
VERSION = "0.4.0"
DIGEST = "sha256:" + "b" * 64
IMAGE = f"{REGISTRY}@{DIGEST}"
ID = "sha256:" + "c" * 64


def workflow(name):
    text = (WORKFLOWS / name).read_text()
    return yaml.load(text, Loader=yaml.BaseLoader)


@pytest.fixture
def helper():
    path = PACKAGE / "scripts" / "verify-release-image.py"
    spec = importlib.util.spec_from_file_location("verify_release_image", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_uses_only_atlas_outputs_and_exact_checkout():
    release = workflow("release-please.yml")
    assert set(release["on"]) == {"push"}
    assert release["on"]["push"]["branches"] == ["main"]
    assert release["permissions"] == {"contents": "read"}
    jobs = release["jobs"]
    outputs = jobs["release-please"]["outputs"]
    assert set(outputs) == {"atlas_created", "atlas_sha", "atlas_tag", "atlas_version"}
    for key, name in {
        "atlas_created": "release_created", "atlas_sha": "sha",
        "atlas_tag": "tag_name", "atlas_version": "version",
    }.items():
        assert f"knowledge-atlas--{name}" in outputs[key]
    publish = jobs["atlas-image"]
    assert publish["needs"] == "release-please"
    assert "needs.release-please.outputs.atlas_created == 'true'" in publish["if"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert publish["runs-on"] == "ubuntu-24.04-arm"
    assert "atlas_version" in publish["concurrency"]["group"]
    assert publish["concurrency"]["cancel-in-progress"] == "false"
    assert publish["steps"][0]["with"] == {
        "ref": "${{ needs.release-please.outputs.atlas_sha }}",
        "persist-credentials": "false",
    }
    steps = "\n".join(step.get("run", "") for step in publish["steps"])
    assert "--build-arg ATLAS_VERSION=" in steps
    assert "--build-arg ATLAS_REVISION=" in steps
    assert "git rev-parse HEAD" in steps
    gate = next(step for step in publish["steps"] if step.get("uses") == "./.github/actions/plugin-image-security")
    assert gate["with"]["revision"] == "${{ needs.release-please.outputs.atlas_sha }}"
    assert "GITHUB_SHA" not in gate.get("env", {})


def test_pr_scans_candidate_without_push_permissions():
    checks = workflow("knowledge-atlas.yml")
    assert checks["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in checks["on"]
    assert {
        "tools/image_security/**", ".github/actions/plugin-image-security/**",
    } <= set(checks["on"]["pull_request"]["paths"])
    job = checks["jobs"]["atlas-image"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert not job.get("permissions")
    for step in job["steps"]:
        assert "docker login" not in step.get("run", "")
        assert "docker push" not in step.get("run", "")
    for name in ("knowledge-atlas.yml", "release-please.yml"):
        steps = workflow(name)["jobs"]["atlas-image"]["steps"]
        gate = next(step for step in steps if step.get("uses") == "./.github/actions/plugin-image-security")
        assert gate["with"]["producer"] == "knowledge-atlas"
        assert gate["with"]["platform"] == "linux/arm64"
        assert gate["with"]["image"] == "${{ steps.build.outputs.image }}"
        assert gate["with"]["out"] == "${{ runner.temp }}/atlas-security"
        assert "continue-on-error" not in gate


def test_native_job_qualifies_public_digest_separately():
    release = workflow("release-please.yml")
    job = release["jobs"]["atlas-verify"]
    assert "atlas-image" in job["needs"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
    steps = "\n".join(step.get("run", "") for step in job["steps"])
    assert "verify-release-image.py" in steps
    assert "needs.atlas-image.outputs.digest" in json.dumps(job)
    assert "uv run --no-sync pytest -q tests/integration/test_multi_project_runtime.py" in steps
    assert "--runtime-config" in steps and "--evidence-dir" in steps
    assert "docker login" not in steps
    compose = yaml.load((PACKAGE / "docker/compose.yaml").read_text(), Loader=yaml.BaseLoader)
    assert compose["services"]["atlas"]["image"].startswith(f"{REGISTRY}:{VERSION}")


@pytest.mark.parametrize(("filename", "job"), [
    ("knowledge-atlas.yml", "atlas-image"),
    ("release-please.yml", "atlas-image"),
    ("release-please.yml", "atlas-verify"),
])
def test_atlas_selects_classic_store_before_docker_work(filename, job):
    steps = workflow(filename)["jobs"][job]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[1] == {"uses": "./.github/actions/plugin-image-security/setup-docker"}


@pytest.mark.skipif(not shutil.which("jq"), reason="runner setup uses jq")
@pytest.mark.parametrize("case", ["self-hosted", "classic", "switch", "fresh", "invalid", "restart-failed", "wrong-store"])
def test_hosted_image_store_setup(tmp_path, case):
    action = WORKFLOWS.parent / "actions/plugin-image-security/setup-docker/action.yml"
    step, = yaml.load(action.read_text(), Loader=yaml.BaseLoader)["runs"]["steps"]
    assert step["shell"] == "bash"
    old = {"debug": True, "features": {"containerd-snapshotter": True, "other": True}}
    config = tmp_path / "daemon.json"
    if case != "fresh":
        config.write_text(json.dumps(old))
    trace = tmp_path / "calls"
    shim = tmp_path / "command.py"
    shim.write_text(r"""import os
from pathlib import Path
import shutil
import sys

tool, *args = sys.argv[1:]
case = os.environ["SETUP_CASE"]
root = Path(os.environ["RUNNER_TEMP"])
config = root / "daemon.json"
restarted = root / "restarted"
with (root / "calls").open("a") as trace:
    trace.write(tool + " " + args[0] + "\n")

if tool == "docker":
    assert args[0:2] == ["info", "--format"]
    if case == "classic" or (restarted.exists() and case != "wrong-store"):
        print('overlay2 [["Backing Filesystem","extfs"]]')
    else:
        print('overlayfs [["driver-type","io.containerd.snapshotter.v1"]]')
else:
    assert args.pop(0) == "-n"
    command, *args = args
    if command in ("test", "cat"):
        assert args[-1] == "/etc/docker/daemon.json"
        if command == "test":
            assert args[0] == "-e"
            sys.exit(not config.exists())
        print(config.read_text(), end="")
    elif command == "dockerd":
        assert args[:2] == ["--validate", "--config-file"]
        (root / "validated").write_bytes(Path(args[2]).read_bytes())
        sys.exit(case == "invalid")
    elif command == "install":
        assert args[:3] == ["-D", "-m", "0600"]
        assert args[4] == "/etc/docker/daemon.json"
        assert (root / "validated").read_bytes() == Path(args[3]).read_bytes()
        shutil.copyfile(args[3], config)
    else:
        assert (command, args) == ("systemctl", ["restart", "docker.service"])
        if case == "restart-failed":
            sys.exit(3)
        restarted.touch()
""")
    for tool in ("docker", "sudo"):
        path = tmp_path / tool
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{shim}" {tool} "$@"\n')
        path.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
           "RUNNER_OS": "Linux", "GITHUB_ACTIONS": "true", "SETUP_CASE": case,
           "RUNNER_ENVIRONMENT": "self-hosted" if case == "self-hosted" else "github-hosted"}
    result = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)
    if case == "self-hosted":
        assert result.returncode != 0
        assert not trace.exists()
    elif case == "classic":
        assert result.returncode == 0, result.stderr
        assert trace.read_text().splitlines() == ["docker info"]
        assert json.loads(config.read_text()) == old
    elif case == "invalid":
        assert result.returncode != 0
        assert not (tmp_path / "restarted").exists()
        assert json.loads(config.read_text()) == old
    else:
        assert (result.returncode == 0) == (case in {"switch", "fresh"}), result.stderr
        expected = {} if case == "fresh" else old
        expected.setdefault("features", {})["containerd-snapshotter"] = False
        assert json.loads(config.read_text()) == expected
        if case != "restart-failed":
            assert trace.read_text().splitlines()[-1] == "docker info"


def test_publisher_checks_again_and_verifies_before_the_only_push():
    steps = workflow("release-please.yml")["jobs"]["atlas-image"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/plugin-image-security")
    publish = next(step for step in steps[index + 1:] if "docker push " in step.get("run", ""))
    code = publish["run"]
    assert code.count("docker push ") == 1
    assert code.index("imagetools inspect") < code.index("gate.py verify") < code.index("docker push ")
    assert "always()" not in publish.get("if", "")
    assert "continue-on-error" not in publish
    assert any("always()" in step.get("if", "") and "upload-artifact@" in step.get("uses", "") for step in steps)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("version", "version"), ("revision", "revision"), ("source", "source"),
        ("digest", "digest"), ("platform", "platform"), ("unavailable", "manifest unknown"),
        ("denied", "denied"), ("handshake", "application version"),
        ("", None),
    ],
)
def test_public_image_runs_anonymous_pull_and_authenticated_handshake(helper, monkeypatch, capsys, change, message):
    options = {
        "Id": ID, "Os": "linux", "Architecture": "arm64", "RepoDigests": [IMAGE],
        "Config": {"Labels": {
            "org.opencontainers.image.source": "https://github.com/sergesha/claude-essentials",
            "org.opencontainers.image.version": VERSION,
            "org.opencontainers.image.revision": REVISION,
        }},
    }
    if change in {"version", "revision", "source"}:
        options["Config"]["Labels"][f"org.opencontainers.image.{change}"] = "changed"
    if change == "digest":
        options["RepoDigests"] = [f"{REGISTRY}@{ID}"]
    if change == "platform":
        options["Architecture"] = "amd64"
    calls = []

    def command(argv, **kwargs):
        assert argv[0:2] == ["docker", "--config"]
        assert "ghcr.io" in json.loads((Path(argv[2]) / "config.json").read_text())["auths"]
        calls.append(argv[3:])
        if argv[3] == "pull":
            if change in {"unavailable", "denied"}:
                raise subprocess.CalledProcessError(1, argv, stderr=message)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[3:5] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([options]), "")
        assert argv[3] == "run"
        assert argv[argv.index("--network") + 1] == "none"
        assert IMAGE in argv
        result = {"application_version": "0.3.4" if change == "handshake" else VERSION}
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.setattr(helper.subprocess, "run", command)
    outcome = helper.main(["--image", IMAGE, "--version", VERSION, "--revision", REVISION])
    captured = capsys.readouterr()
    assert calls[0] == ["pull", "--platform", "linux/arm64", IMAGE]
    if message:
        assert outcome != 0
        assert message in captured.err.lower()
    else:
        assert outcome == 0
        assert json.loads(captured.out)["config_id"] == ID
        assert len(calls) == 3


def test_tag_only_image_is_rejected_without_docker(helper, monkeypatch, capsys):
    def unexpected_command(*args, **kwargs):
        pytest.fail("Docker must not run for a mutable tag")

    monkeypatch.setattr(helper.subprocess, "run", unexpected_command)
    assert helper.main(["--image", f"{REGISTRY}:{VERSION}", "--version", VERSION, "--revision", REVISION]) != 0
    assert "digest" in capsys.readouterr().err.lower()

@pytest.mark.parametrize("selection", ["event", "checkout", "mismatch"])
def test_shared_scan_validates_checkout_and_uses_selected_revision(tmp_path, selection):
    action = WORKFLOWS.parent / "actions/plugin-image-security/action.yml"
    run = yaml.load(action.read_text(), Loader=yaml.BaseLoader)["runs"]["steps"][0]["run"]
    head = subprocess.check_output(["git", "-C", str(PACKAGE.parent), "rev-parse", "HEAD"], text=True).strip()
    event = "e" * 40
    observed = tmp_path / "observed-revision"
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$GITHUB_SHA" > "$ATLAS_TEST_SCAN_SHA"\n')
    fake.chmod(0o755)
    selected = {"event": "", "checkout": head, "mismatch": "f" * 40}[selection]
    output = tmp_path / "output"
    env = {
        **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GITHUB_ACTION_PATH": str(action.parent),
        "GITHUB_SHA": event, "GITHUB_WORKSPACE": str(PACKAGE.parent), "GITHUB_OUTPUT": str(output),
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "PRODUCER": "knowledge-atlas",
        "IMAGE": ID, "PLATFORM": "linux/arm64", "OUT": str(tmp_path / "report"), "REVISION": selected,
        "ATLAS_TEST_SCAN_SHA": str(observed),
    }
    result = subprocess.run(["bash", "-ec", run], env=env, capture_output=True, text=True)
    if selection == "mismatch":
        assert result.returncode and not observed.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert observed.read_text().strip() == (event if selection == "event" else head)


# The workflow's real shell runs with a disposable Docker/scan boundary. The
# shared gate owns vulnerability decisions; these cases exercise their effect
# on the Atlas publisher and the previously recorded registry identity.
PUBLISH_COMMAND = """
import json
import os
from pathlib import Path
import sys

tool, *args = sys.argv[1:]
case = os.environ["ATLAS_TEST_CASE"]
image = os.environ["ATLAS_REPOSITORY"] + ":" + os.environ["ATLAS_VERSION"]
config = os.environ["IMAGE_ID"]
digest = os.environ["ATLAS_TEST_DIGEST"]
published = os.environ["ATLAS_TEST_MANIFEST"]
replacement = os.environ["ATLAS_TEST_REPLACEMENT"]
replacement_digest = os.environ["ATLAS_TEST_REPLACEMENT_DIGEST"]
with Path(os.environ["ATLAS_TEST_TRACE"]).open("a") as trace:
    print(tool, *args, file=trace)

if tool == "git":
    assert args == ["rev-parse", "HEAD"]
    print(os.environ["ATLAS_REVISION"])
elif tool == "python3":
    assert args == [
        "tools/image_security/gate.py", "verify", "--image", image, "--platform", "linux/arm64",
        "--revision", os.environ["ATLAS_REVISION"], "--out", os.environ["RUNNER_TEMP"] + "/atlas-security",
    ]
    assert os.environ["GITHUB_SHA"] == os.environ["ATLAS_REVISION"]
    if case in {"blocked", "incomplete", "stale", "substituted"}:
        print(case, file=sys.stderr)
        sys.exit(2)
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    if args[3] == image:
        if case == "denied":
            print("ERROR: denied", file=sys.stderr)
            sys.exit(1)
        trace = Path(os.environ["ATLAS_TEST_TRACE"]).read_text()
        pushed = "docker push" in trace
        if case == "missing-child" and not pushed:
            if args[-1] != "--raw":
                print("ERROR: manifest unknown", file=sys.stderr)
                sys.exit(1)
            print(json.dumps({
                "schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [{"digest": "sha256:" + "0" * 64, "platform": {"os": "linux", "architecture": "arm64"}}],
            }), end="")
        elif case not in {"reuse", "conflict"} and not pushed:
            print(f"ERROR: {image}: not found", file=sys.stderr)
            sys.exit(1)
        elif args[-1] == "--raw":
            print(replacement if case == "tag-switched" else published, end="")
        else:
            print(json.dumps({"digest": replacement_digest if case == "tag-switched" else digest}))
    elif args[-1] == "--raw":
        print(replacement if args[3].endswith(replacement_digest) else published, end="")
    else:
        print(json.dumps({
            "architecture": "arm64", "os": "linux", "config": {"Labels": {
                "org.opencontainers.image.source": "https://github.com/sergesha/claude-essentials",
                "org.opencontainers.image.version": os.environ["ATLAS_VERSION"],
                "org.opencontainers.image.revision": os.environ["ATLAS_REVISION"],
            }},
        }))
elif args[0] == "tag":
    assert args == ["tag", config, image]
elif args[0] == "push":
    assert args == ["push", image]
    print(f"{os.environ['ATLAS_VERSION']}: digest: {digest} size: {len(published)}")
else:
    raise AssertionError(args)
"""


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "jq", "sha256sum")),
    reason="publisher uses bash, jq and sha256sum",
)
@pytest.mark.parametrize("case", ["blocked", "incomplete", "stale", "substituted", "denied", "conflict", "reuse", "publish",
         "missing-child", "tag-switched"])
def test_publisher_refuses_unsafe_attempts_and_reuses_exact_receipt(tmp_path, case):
    steps = workflow("release-please.yml")["jobs"]["atlas-image"]["steps"]
    lookup = next(step["run"] for step in steps if step.get("id") == "existing")
    publish = next(step["run"] for step in steps if step.get("id") == "publish")
    trace = tmp_path / "commands.txt"
    shim = tmp_path / "command.py"
    shim.write_text(PUBLISH_COMMAND)
    commands = tmp_path / "bin"
    commands.mkdir()
    for tool in ["docker", "git", "python3"]:
        path = commands / tool
        path.write_text(f'#!/bin/sh\nexec "$ATLAS_TEST_PYTHON" "$ATLAS_TEST_SHIM" {tool} "$@"\n')
        path.chmod(0o755)
    evidence = tmp_path / "atlas-publication"
    evidence.mkdir()
    output = tmp_path / "outputs"
    output.touch()
    tag = f"knowledge-atlas-v{VERSION}"

    manifest = {"schemaVersion": 2, "config": {"digest": ID}}
    published = json.dumps(manifest, separators=(",", ":"))
    replacement = json.dumps({**manifest, "annotations": {"name": "replacement"}}, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(published.encode()).hexdigest()
    replacement_digest = "sha256:" + hashlib.sha256(replacement.encode()).hexdigest()
    expected = {
        "digest": digest, "config_id": ID, "version": VERSION, "revision": REVISION,
        "platform": "linux/arm64", "tag": tag,
    }
    if case in {"reuse", "conflict"}:
        prior = tmp_path / "atlas-prior-publication" / "atlas-publication-1"
        prior.mkdir(parents=True)
        receipt = expected if case == "reuse" else {**expected, "digest": "sha256:" + "d" * 64}
        (prior / "publication.json").write_text(json.dumps(receipt))
    env = {
        **os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output), "ATLAS_REPOSITORY": REGISTRY, "ATLAS_TAG": tag,
        "ATLAS_VERSION": VERSION, "ATLAS_REVISION": REVISION, "IMAGE_ID": ID,
        "ATLAS_TEST_CASE": case, "ATLAS_TEST_DIGEST": digest, "ATLAS_TEST_MANIFEST": published,
        "ATLAS_TEST_REPLACEMENT": replacement, "ATLAS_TEST_REPLACEMENT_DIGEST": replacement_digest,
        "ATLAS_TEST_PYTHON": sys.executable,
        "ATLAS_TEST_SHIM": str(shim), "ATLAS_TEST_TRACE": str(trace),
    }

    result = subprocess.run(["bash", "-ec", lookup], env=env, capture_output=True, text=True)
    if result.returncode == 0 and not output.read_text():
        result = subprocess.run(["bash", "-ec", publish], env=env, capture_output=True, text=True)

    calls = trace.read_text().splitlines()
    pushes = [call for call in calls if call.startswith("docker push ")]
    assert len(pushes) == (1 if case in {"publish", "tag-switched"} else 0)
    if case == "missing-child":
        assert calls == [f"docker buildx imagetools inspect {REGISTRY}:{VERSION} --raw"]
    if case == "tag-switched":
        assert f"docker buildx imagetools inspect {REGISTRY}@{digest} --raw" in calls
        assert digest in result.stderr and replacement_digest in result.stderr
    if case in {"reuse", "publish"}:
        assert result.returncode == 0, result.stderr
        assert json.loads((evidence / "publication.json").read_text()) == expected
        assert f"digest={digest}" in output.read_text()
    else:
        assert result.returncode != 0
        assert not (evidence / "publication.json").exists()
        assert not output.read_text()
