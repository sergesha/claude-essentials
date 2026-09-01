from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
CONTROLLED_EFFECT = Path(__file__).parent / "fixtures/controlled_effect_executable.py"

ACTIVE_ROOT_PATHS = (
    ".claude-plugin",
    ".codex-plugin",
    ".mcp.json",
    "README.md",
    "docs/DESIGN.md",
    "hooks",
    "recipes",
    "scripts",
    "skills",
    "engine/pyproject.toml",
    "engine/src/lockstep",
)
STAGED_DELIVERY_PATHS = (
    ".claude-plugin",
    ".codex-plugin",
    ".mcp.json",
    "README.md",
    "docs/DESIGN.md",
    "hooks",
    "scripts",
    "skills",
    "engine/pyproject.toml",
    "engine/uv.lock",
    "engine/src/lockstep",
)
RETIRED_BYTES = (
    b"_subcall",
    b"lockstep.subcalls",
    b"_subcall_wrapper.py",
    b"Subcalls (v2)",
    b"runners.yaml",
    b"LOCKSTEP_RUNNER",
    b"RunnerSpec",
    b"load_runners",
    b"build_argv",
    b"lockstep.runtime.runners",
    b"peak_parallel_subcalls",
)
HISTORICAL_ONLY = (
    "CHANGELOG.md",
    "docs/superpowers/specs/2026-08-19-codex-claude-parity-design.md",
)
EXACT_DOCUMENTED_CLI = (
    "recipe init NAME",
    "recipe compile NAME",
    "recipe check [NAME | --all]",
    "recipe diff NAME",
    "recipe render NAME --view workflow|generated",
    "recipe estimate NAME [--json]",
    "template list",
    "template show TEMPLATE NAME",
    "template init TEMPLATE NAME",
)
EXACT_DOCUMENTED_MCP = (
    "recipe_init",
    "recipe_compile",
    "recipe_check",
    "recipe_diff",
    "recipe_render",
    "recipe_estimate",
    "template_list",
    "template_show",
)


def _tracked() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True
    )
    return set(result.stdout.splitlines())


def _files(root: Path, paths: tuple[str, ...]) -> tuple[Path, ...]:
    found: list[Path] = []
    for relative in paths:
        target = root / relative
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(path for path in target.rglob("*") if path.is_file())
    if root == ROOT:
        tracked = _tracked()
        found = [path for path in found if path.relative_to(root).as_posix() in tracked]
    return tuple(sorted(found))


def _assert_active_bytes_are_retired(root: Path, paths: tuple[str, ...]) -> None:
    violations: list[str] = []
    for path in _files(root, paths):
        content = path.read_bytes()
        lower = content.lower()
        matched = [term.decode() for term in RETIRED_BYTES if term in content]
        if b"fractal" in lower or b"subcall" in lower:
            matched.append("active fractal/subcall prose")
        if matched:
            violations.append(
                f"{path.relative_to(root)}: {', '.join(sorted(set(matched)))}"
            )
        for historical in HISTORICAL_ONLY:
            if historical.encode() in content:
                violations.append(
                    f"{path.relative_to(root)} links historical-only {historical}"
                )
    assert violations == []


def _assert_no_legacy_runner_importers(files: dict[str, bytes]) -> None:
    legacy = "lockstep.runtime.runners"
    violations: list[str] = []
    for name, content in sorted(files.items()):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(content, filename=name)
        parts = Path(name).parts
        try:
            lockstep_index = parts.index("lockstep")
        except ValueError:
            package: tuple[str, ...] = ()
        else:
            package = tuple(parts[lockstep_index:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
                if any(item == legacy or item.startswith(f"{legacy}.") for item in imported):
                    violations.append(f"{name}:{node.lineno}: import {', '.join(imported)}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if not package or node.level > len(package):
                        resolved = ()
                    else:
                        resolved = package[: len(package) - node.level + 1]
                    if node.module:
                        resolved = (*resolved, *node.module.split("."))
                    module = ".".join(resolved)
                else:
                    module = node.module or ""
                imported = tuple(alias.name for alias in node.names)
                if module == legacy or (
                    module == "lockstep.runtime" and "runners" in imported
                ):
                    violations.append(
                        f"{name}:{node.lineno}: from {'.' * node.level}{node.module or ''} "
                        f"import {', '.join(imported)}"
                    )
    assert violations == []


def _active_file_bytes(root: Path, paths: tuple[str, ...]) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in _files(root, paths)
    }


def _assert_active_guidance(root: Path) -> None:
    for relative in (
        "README.md",
        "docs/DESIGN.md",
        "skills/lockstep/SKILL.md",
        "skills/lockstep-author/SKILL.md",
    ):
        text = " ".join((root / relative).read_text().lower().split())
        assert "local unsandboxed single-user" in text, relative
        assert "ambient os-user authority" in text, relative
        assert "tcb" in text or "trusted computing base" in text, relative
        assert "not security confinement" in text, relative
        assert "no constrained-runner, broker, or sandbox guarantee" in text, relative
        assert "marker-free" in text and "manual yamlgraph" in text, relative
        assert "reviewed-change" in text and "parallel-review" in text, relative
        assert "no configuration or report text grants authority" in text, relative
        assert "configuration is authority" not in text, relative
        assert "report text is authority" not in text, relative
        assert "configuration can grant authority" not in text, relative
        assert "report text can grant authority" not in text, relative
        assert (
            "configuration, manifests, templates, recipes, reports, artifact "
            "digests, run ids, pass strings, and host markers are non-authoritative"
            in text
        ), relative
        assert (
            "ambient os-user authority describes process power and tcb exposure, "
            "not an authorization source"
            in text
        ), relative
        assert (
            "managed and pinned os-user execution requires an exact owner-selected "
            "runtime grant, resolved and revalidated at commitment"
            in text
        ), relative
        assert (
            "publication separately requires a fresh exact bearer bound to the "
            "named commitment"
            in text
        ), relative
        assert "actual authority comes only from ambient os capabilities" not in text, relative
        assert (
            "ambient os capabilities and explicit runtime-validated owner consent "
            "are the authority sources"
            not in text
        ), relative
        assert (
            "only ambient os capabilities and explicit runtime-validated owner "
            "consent authorize effects"
            not in text
        ), relative

    operator_skill = " ".join(
        (root / "skills/lockstep/SKILL.md").read_text().lower().split()
    )
    assert "terminal `completed`, `escalated`, or `aborted` status" in operator_skill
    assert "terminal pass, fail, error, or aborted" not in operator_skill
    assert all(
        tool in operator_skill
        for tool in (
            "`scenario_recover`",
            "`scenario_wait`",
            "`scenario_history`",
            "`scenario_events`",
        )
    )
    assert "`scenario_status` does not return artifact references" in operator_skill
    assert "`lockstep consent issue --run run_id --step step_id`" in operator_skill
    assert "`lockstep consent accept`" in operator_skill
    assert "terminal status and validated artifact references" not in operator_skill

    readme = (root / "README.md").read_text()
    assert "/plugin marketplace add sergesha/claude-essentials" in readme
    assert "/plugin install lockstep@claude-essentials" in readme
    assert (
        "codex plugin marketplace add /absolute/path/to/claude-essentials --json"
        in readme
    )
    assert "codex plugin add lockstep@claude-essentials --json" in readme
    assert "uv run --project engine --no-sync lockstep doctor" in readme
    assert "`lockstep doctor` after installation" not in readme


def _assert_documented_authoring_grammar(root: Path) -> None:
    for relative in (
        "README.md",
        "docs/DESIGN.md",
        "skills/lockstep/SKILL.md",
        "skills/lockstep-author/SKILL.md",
    ):
        text = (root / relative).read_text()
        assert all(item in text for item in EXACT_DOCUMENTED_CLI), relative
        assert all(item in text for item in EXACT_DOCUMENTED_MCP), relative
        assert "template_init" not in text, relative
        assert "recipe init --template" not in text, relative
        assert not re.search(
            r"(?:lockstep\s+)?(?:recipe|template)\s+\w+[^\n`]*--format",
            text,
        ), relative
        assert not re.search(
            r"(?:lockstep\s+)?recipe\s+(?:init|compile|check|diff|render|estimate)"
            r"\s+(?:[^\s`]*[/\\][^\s`]*|[^\s`]+\.ya?ml)(?:\s|`|$)",
            text,
        ), relative
        for line in text.splitlines():
            match = re.search(r"\blockstep\s+(?:recipe|template)\s+[^`\n]+", line)
            if match is None:
                continue
            command = " ".join(match.group(0).split())
            assert "recipe init --template" not in command, relative
            assert "--format" not in command, relative


def _clean_env(**updates: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("LOCKSTEP_") or name in {
            "CODEX_HOME",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
        }:
            env.pop(name)
    env.update(updates)
    return env


_FLOW_PROBE = textwrap.dedent(
    r"""
    import hashlib
    import importlib.metadata
    import importlib.resources
    import importlib.util
    import json
    import logging
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from lockstep.runtime import sessions
    from lockstep.runtime.effects.models import AcceptDescriptor
    from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
    from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
    from lockstep.runtime.engine import Engine
    from lockstep.runtime.errors import LockstepError
    from lockstep.runtime.providers.codex import CodexRunnerAdapter
    from lockstep.runtime.service import preflight_recipe
    from lockstep.templates import install_template

    root = Path(os.environ["LOCKSTEP_PROBE_ROOT"])
    controlled = Path(os.environ["LOCKSTEP_CONTROLLED_EFFECT"]).resolve(strict=True)
    expected_environment = Path(os.environ["LOCKSTEP_EXPECTED_ENV_ROOT"]).resolve(strict=True)
    logging.disable(logging.CRITICAL)

    def wait(command, project, run_id, predicate, timeout=30.0):
        projection = Engine.observe(command.state_dir, command.recipes_dir)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if command._pump_failure is not None:
                    raise command._pump_failure
                value = projection.status(run_id, str(project))
                if predicate(value):
                    return value
                time.sleep(0.02)
        finally:
            projection.close()
        raise AssertionError(f"timed out waiting for run {run_id}")

    def config(runtime_root, parallel=False):
        runtime_root.mkdir(parents=True)
        codex_home = runtime_root / "codex-home"
        pinned_home = runtime_root / "pinned-home"
        private_tmp = runtime_root / "tmp"
        for path in (codex_home, pinned_home, private_tmp):
            path.mkdir(mode=0o700)
        auth = codex_home / "auth.json"
        auth.write_text("{}")
        auth.chmod(0o600)
        if parallel:
            (private_tmp / "lockstep-controlled-two-process-barrier").mkdir()
        environment = {
            "PATH": os.environ["PATH"],
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(private_tmp),
        }
        common = {
            "executable": str(controlled),
            "model": "task12c-installed-contract",
            "cli_version": "task12c-installed-contract",
            "permission_profile": {"sandbox": "workspace-write", "approval": "never"},
            "environment": environment,
        }
        return {
            "codex": {**common, "codex_home": str(codex_home)},
            "pinned": {
                **common,
                "codex_home": str(pinned_home),
                "pinned_permission_profile": "task12c-installed-pinned",
            },
        }

    def provision(project, recipe, owner, runtime_root, parallel=False):
        recipes = project / ".lockstep/recipes"
        authorized = preflight_recipe(recipes, recipe)
        index = RuntimeRequirementIndex.for_authorized_closures(
            (authorized,), project_identity=str(project.resolve())
        )
        bindings = config(runtime_root, parallel=parallel)
        refusal_owner = owner.parent / f"{owner.name}-configuration-only"
        configuration_only = provision_runtime_snapshot(
            state_dir=refusal_owner,
            codex=bindings["codex"],
            pinned=bindings["pinned"],
            replacement_keys=(),
            index=index,
            project=project,
        )
        assert configuration_only.grants == ()
        refusal = Engine.command(refusal_owner, recipes)
        try:
            try:
                refusal.start(recipe, {}, str(project))
            except LockstepError:
                pass
            else:
                raise AssertionError("configuration alone granted runtime authority")
        finally:
            refusal.close()
        granted = provision_runtime_snapshot(
            state_dir=owner,
            codex=bindings["codex"],
            pinned=bindings["pinned"],
            replacement_keys=tuple(item.grant_selection_key for item in index.requirements),
            index=index,
            project=project,
        )
        assert {grant.grant_selection_key for grant in granted.grants} == {
            item.grant_selection_key for item in index.requirements
        }
        assert len(granted.grants) == len(index.requirements)
        return index

    def assert_manual_cli(project):
        checked = subprocess.run(
            [sys.executable, "-m", "lockstep", "recipe", "check", "manual"],
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        estimated = subprocess.run(
            [sys.executable, "-m", "lockstep", "recipe", "estimate", "manual", "--json"],
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
        )
        assert estimated.returncode == 0, estimated.stdout + estimated.stderr
        payload = json.loads(estimated.stdout)
        assert payload["schema"] == "lockstep.structural-estimate/v1"
        return payload

    def pending_accept(command, project, run_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if command._pump_failure is not None:
                raise command._pump_failure
            with command._admission_recovery_lock:
                command.runtime.bind(command.catalog.get(run_id))
                snapshot = command.runtime.snapshot(run_id, subgraphs=True)
            descriptors = [
                descriptor
                for interrupt in snapshot.pending
                if isinstance(
                    descriptor := command._protected_interrupt_descriptor(interrupt),
                    AcceptDescriptor,
                )
            ]
            if descriptors:
                assert len(descriptors) == 1
                return descriptors[0].logical_id
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for acceptance in run {run_id}")

    def accept(command, project, run_id, step):
        preview = command.preview_publication_consent(run_id, step, project=str(project))
        issued = command.issue_publication_consent(
            run_id, step, preview["digest"], project=str(project)
        )
        stored = command.authority.inspect_token(issued.token)
        assert stored.commitment.to_dict() == preview
        assert issued.commitment_digest == preview["digest"] == stored.commitment.digest
        assert issued.consent_ref == stored.consent_ref
        assert stored.receipt_digest is None
        command.scenario_accept_artifact(issued.token, project=str(project))
        redeemed = command.authority.inspect_token(issued.token)
        assert redeemed.commitment == stored.commitment
        assert redeemed.receipt_digest is not None
        binding = command.catalog.get(run_id)
        acceptances = [
            record
            for record in command.effects.list_for_thread(binding.thread_id)
            if record.effect_kind == "accept"
            and record.result is not None
            and record.result.consent_ref == issued.consent_ref
        ]
        assert len(acceptances) == 1
        result = acceptances[0].result
        assert result is not None
        assert result.receipt_digest == redeemed.receipt_digest
        assert result.artifact_ref == stored.commitment.artifact_ref
        assert result.artifact_digest == stored.commitment.artifact_digest
        assert result.destination == stored.commitment.destination
        assert acceptances[0].descriptor_digest == stored.commitment.descriptor_digest
        return issued, stored.commitment, result

    def assert_observations(owner, recipes, project, run_id, terminal=False):
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline:
            projection = Engine.observe(owner, recipes)
            try:
                status = projection.status(run_id, str(project))
                assert projection.history(run_id, str(project))
                assert projection.events(run_id, str(project))
                waited = projection.wait(run_id, 1, str(project))
                assert isinstance(waited["changed"], bool) and waited["revision"]
                if terminal:
                    assert status["status"] == "completed"
                return
            except LockstepError as exc:
                last_error = exc
                time.sleep(0.02)
            finally:
                projection.close()
        raise AssertionError("public observations did not become readable") from last_error

    def reviewed():
        base = root / "reviewed"
        project = base / "project"
        project.mkdir(parents=True)
        install_template("reviewed-change", "release", project, state_dir=base / "authoring")
        recipes = project / ".lockstep/recipes"
        index = provision(project, "release", base / "owner", base / "runtime")
        assert sorted(item.runner_selector for item in index.requirements) == ["codex", "pinned"]
        command = Engine.command(base / "owner", recipes)
        try:
            run_id = command.start("release", {}, str(project))["run_id"]
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "plan",
            )
            session = "installed-reviewed-worker"
            assert sessions.touch(base / "owner", run_id, session, 30) == "bound"
            plan = project / ".lockstep/plan.md"
            plan.write_text("# Goal\nShip.\n\n# Acceptance Criteria\nPass.\n\n# Steps\nReview.\n")
            command.scenario_done(run_id, "plan", {"path": ".lockstep/plan.md"}, session_id=session, project=str(project))
            pinned_marker = base / "pinned.marker"
            tests = project / "tests"
            tests.mkdir()
            (tests / "test_installed.py").write_text(
                "import sys\nfrom pathlib import Path\n\n"
                "def test_installed_pinned():\n"
                f"    Path({str(pinned_marker)!r}).write_text(sys.executable)\n"
            )
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "tests",
            )
            command.scenario_done(run_id, "tests", {"path": "tests/test_installed.py"}, session_id=session, project=str(project))
            source = project / "src"
            source.mkdir()
            (source / "installed.py").write_text("VALUE = 4\n")
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "implement",
            )
            command.scenario_done(run_id, "implement", {"path": "src/installed.py"}, session_id=session, project=str(project))
            step = pending_accept(command, project, run_id)
            binding = command.catalog.get(run_id)
            records = command.effects.list_for_thread(binding.thread_id)
            managed = [record for record in records if record.effect_kind == "managed"]
            pinned = [record for record in records if record.effect_kind == "verify"]
            assert len(managed) == len(pinned) == 1
            assert managed[0].phase == pinned[0].phase == "delivered"
            assert Path(pinned_marker.read_text()).absolute().is_relative_to(
                expected_environment
            )
            artifact = command.artifacts.read(managed[0].result.artifact_refs[0])
            expected = command.blobs.read(artifact.blob)
            assert expected.startswith(b"# Findings\nControlled evidence-backed review.\n")
            assert not (project / ".lockstep/review.md").exists()
            assert type(command._runtime_execution_composition.runners.codex) is CodexRunnerAdapter
            assert command._runtime_execution_composition.runners.codex.spawn_count == 1
            assert command._runtime_execution_composition.runners.pinned.spawn_count == 1
            assert_observations(base / "owner", recipes, project, run_id)
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            issued, commitment, accepted = accept(reopened, project, run_id, step)
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            assert (project / ".lockstep/review.md").read_bytes() == expected
            assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
            binding = reopened.catalog.get(run_id)
            acceptance = [
                record for record in reopened.effects.list_for_thread(binding.thread_id)
                if record.effect_kind == "accept"
            ]
            assert len(acceptance) == 1 and acceptance[0].result is not None
            assert acceptance[0].result == accepted
            assert commitment.producer_effect_id == managed[0].effect_id
            assert commitment.artifact_ref == str(artifact.ref)
            assert commitment.artifact_digest == artifact.blob.digest
            assert commitment.destination == ".lockstep/review.md"
            assert issued.consent_ref == accepted.consent_ref
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()

    def parallel():
        base = root / "parallel"
        project = base / "project"
        project.mkdir(parents=True)
        (project / "tracked.txt").write_text("parallel bytes\n")
        install_template("parallel-review", "release", project, state_dir=base / "authoring")
        recipes = project / ".lockstep/recipes"
        index = provision(project, "release", base / "owner", base / "runtime", parallel=True)
        assert len(index.requirements) == 2
        assert {item.runner_selector for item in index.requirements} == {"codex"}
        command = Engine.command(base / "owner", recipes)
        try:
            run_id = command.start("release", {}, str(project))["run_id"]
            first_step = pending_accept(command, project, run_id)
            binding = command.catalog.get(run_id)
            managed = [record for record in command.effects.list_for_thread(binding.thread_id) if record.effect_kind == "managed"]
            assert len(managed) == 2 and all(record.phase == "delivered" for record in managed)
            assert command._runtime_execution_composition.runners.codex.spawn_count == 2
            artifacts = [command.artifacts.read(record.result.artifact_refs[0]) for record in managed]
            assert {item.source_path for item in artifacts} == {"security-review.md", "architecture-review.md"}
            intervals = []
            expected = {}
            for artifact in artifacts:
                content = command.blobs.read(artifact.blob)
                expected[artifact.source_path] = content
                lines = content.decode().splitlines()
                intervals.append((int(next(line for line in lines if line.startswith("started_ns: ")).split()[1]), int(next(line for line in lines if line.startswith("ended_ns: ")).split()[1])))
            assert max(start for start, _ in intervals) < min(end for _, end in intervals)
            first_issued, first_commitment, first_result = accept(
                command, project, run_id, first_step
            )
            second_step = pending_accept(command, project, run_id)
            assert second_step != first_step
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            second_issued, second_commitment, second_result = accept(
                reopened, project, run_id, second_step
            )
            assert first_issued.token != second_issued.token
            assert first_issued.consent_ref != second_issued.consent_ref
            assert first_result.receipt_digest != second_result.receipt_digest
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            for source_path, content in expected.items():
                assert (project / ".lockstep" / source_path).read_bytes() == content
            artifacts_by_ref = {str(item.ref): item for item in artifacts}
            commitments = (first_commitment, second_commitment)
            results = (first_result, second_result)
            assert {item.artifact_ref for item in commitments} == set(artifacts_by_ref)
            assert {item.consent_ref for item in results} == {
                first_issued.consent_ref,
                second_issued.consent_ref,
            }
            for commitment, result in zip(commitments, results, strict=True):
                artifact = artifacts_by_ref[commitment.artifact_ref]
                assert commitment.producer_effect_id in {item.effect_id for item in managed}
                assert commitment.artifact_digest == artifact.blob.digest
                assert result.artifact_ref == str(artifact.ref)
                assert result.artifact_digest == artifact.blob.digest
                assert result.destination == commitment.destination
                assert (project / result.destination).read_bytes() == expected[
                    artifact.source_path
                ]
            assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
            with reopened._admission_recovery_lock:
                reopened.runtime.bind(reopened.catalog.get(run_id))
                history = tuple(reopened.runtime.history(run_id))
            join_schedules = [
                snapshot for snapshot in history
                if len(snapshot.next) == 1
                and snapshot.next[0].startswith("parallel-0-join-")
            ]
            assert len(join_schedules) == 1
            joined_values = {
                json.dumps(snapshot.values["reviews_result"], sort_keys=True)
                for snapshot in history if "reviews_result" in snapshot.values
            }
            assert joined_values == {'{"outcome": "PASS", "value": "pass"}'}
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()

    def manual():
        base = root / "manual"
        project = base / "project"
        recipes = project / ".lockstep/recipes"
        recipes.mkdir(parents=True)
        source = project / "src"
        source.mkdir()
        (source / "manual.py").write_text("VALUE = 'manual'\n")
        pinned_marker = base / "pinned.marker"
        tests = project / "tests"
        tests.mkdir()
        (tests / "test_manual.py").write_text(
            "import sys\nfrom pathlib import Path\n\n"
            "def test_manual_pinned():\n"
            f"    Path({str(pinned_marker)!r}).write_text(sys.executable)\n"
        )
        recipe = recipes / "manual.recipe.yaml"
        recipe.write_text(
            "name: manual\n"
            "state: {command: dict, work_request: dict, work_result: dict, request: dict, result: dict, lockstep_outcome: str}\n"
            "nodes:\n"
            "  work:\n"
            "    type: interrupt\n    state_key: work_request\n    resume_key: work_result\n"
            "    idempotent: false\n    message:\n      lockstep_effect:\n"
            "        schema: lockstep.effect/v1\n        kind: manual\n"
            "        logical_id: manual-work\n        runner: null\n        inputs: {}\n"
            "        writes: [src/]\n        artifacts: []\n        deadline_seconds: null\n"
            "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
            "  command:\n    type: passthrough\n    output:\n      command:\n"
            "        schema: lockstep.pinned-command/v1\n"
            "        logical_argv: [python, -m, pytest, -q]\n"
            "        logical_cwd: .\n        result_source: exit\n"
            "  verify:\n    type: interrupt\n    state_key: request\n    resume_key: result\n"
            "    idempotent: false\n    message:\n      lockstep_effect:\n"
            "        schema: lockstep.effect/v1\n        kind: verify\n"
            "        logical_id: manual-tests\n"
            "        runner: {selector: pinned, required_capabilities: [bounded_result, sandbox, workspace]}\n"
            "        inputs: {command: {state_key: command}, snapshot: {runtime_key: current_project_snapshot}}\n"
            "        writes: []\n        artifacts: []\n        deadline_seconds: 120\n"
            "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
            "  done: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
            "edges:\n  - {from: START, to: work}\n  - {from: work, to: command}\n"
            "  - {from: command, to: verify}\n  - {from: verify, to: done}\n"
            "  - {from: done, to: END}\n"
        )
        assert b"x-lockstep-generated" not in recipe.read_bytes()
        manual_estimate = assert_manual_cli(project)
        index = provision(project, "manual", base / "owner", base / "runtime")
        assert len(index.requirements) == 1
        assert index.requirements[0].runner_selector == "pinned"
        command = Engine.command(base / "owner", recipes)
        try:
            result = command.start("manual", {}, str(project))
            run_id = result["run_id"]
            waiting = wait(command, project, run_id, lambda item: item.get("owner") == "worker")
            step = waiting["step"]
            assert_observations(base / "owner", recipes, project, run_id)
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            session = "installed-manual-worker"
            assert sessions.touch(base / "owner", run_id, session, 30) == "bound"
            reopened.scenario_done(
                run_id, step, {"path": "src/manual.py"},
                session_id=session, project=str(project),
            )
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            assert Path(pinned_marker.read_text()).absolute().is_relative_to(
                expected_environment
            )
            assert reopened._runtime_execution_composition.runners.pinned.spawn_count == 1
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()
        return manual_estimate

    reviewed()
    parallel()
    manual_estimate = manual()
    import lockstep
    import lockstep.templates
    from lockstep.mcp import server
    assert {"reviewed-change", "parallel-review"} == set(__import__("lockstep.templates", fromlist=["list_templates"]).list_templates())
    tool_names = {tool.name for tool in server.app._tool_manager.list_tools()}
    assert {
        "recipe_init", "recipe_compile", "recipe_check", "recipe_diff",
        "recipe_render", "recipe_estimate", "template_list", "template_show",
        "scenario_start", "scenario_wait", "scenario_events",
        "scenario_accept_artifact",
    } <= tool_names
    assert "template_init" not in tool_names
    loaded_modules = sorted({
        str(Path(module.__file__).resolve())
        for name, module in sys.modules.items()
        if (name == "lockstep" or name.startswith("lockstep."))
        and getattr(module, "__file__", None)
    })
    package_root = importlib.resources.files("lockstep")
    template_root = importlib.resources.files("lockstep.templates")
    def resource_files(root):
        pending = [root]
        found = []
        while pending:
            item = pending.pop()
            if item.is_file():
                found.append(str(Path(str(item)).resolve()))
            elif item.is_dir():
                pending.extend(item.iterdir())
        return sorted(found)
    distribution = importlib.metadata.distribution("lockstep")
    distribution_files = tuple(distribution.files or ())
    record_files = [
        str(Path(distribution.locate_file(item)).resolve())
        for item in distribution_files
        if str(item).endswith(".dist-info/RECORD")
    ]
    assert len(record_files) == 1 and Path(record_files[0]).is_file()
    print(json.dumps({
        "executable": str(Path(sys.executable).absolute()),
        "lockstep": str(Path(lockstep.__file__).resolve()),
        "templates": str(Path(lockstep.templates.__file__).resolve()),
        "loaded_modules": loaded_modules,
        "package_resource_root": str(Path(str(package_root)).resolve()),
        "package_resource_files": resource_files(package_root),
        "template_resource_root": str(Path(str(template_root)).resolve()),
        "template_resource_files": resource_files(template_root),
        "distribution_root": str(Path(distribution.locate_file("")).resolve()),
        "distribution_record": record_files[0],
        "sys_path": [str(Path(item).resolve()) for item in sys.path if item],
        "manual_estimate": manual_estimate,
        "legacy_runner_importable": importlib.util.find_spec("lockstep.runtime.runners") is not None,
    }, sort_keys=True))
    """
)


def _run_probe(
    python: Path, foreign: Path, controlled: Path, environment_root: Path
) -> dict[str, object]:
    foreign.mkdir(parents=True)
    probe_root = foreign / "probe"
    result = subprocess.run(
        [str(python), "-I", "-c", _FLOW_PROBE],
        cwd=foreign,
        env=_clean_env(
            LOCKSTEP_PROBE_ROOT=str(probe_root),
            LOCKSTEP_CONTROLLED_EFFECT=str(controlled),
            LOCKSTEP_EXPECTED_ENV_ROOT=str(environment_root),
            LOCKSTEP_STATE_DIR=str(probe_root / "ambient-owner"),
            PATH=f"{python.parent}:/usr/bin:/bin",
        ),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def _assert_cli_resource_contract(
    executable: Path, project: Path, env: dict[str, str]
) -> tuple[dict[str, object], ...]:
    project.mkdir(parents=True, exist_ok=True)
    listed = subprocess.run(
        [str(executable), "template", "list"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert listed.stdout == "parallel-review\nreviewed-change\n"
    estimates: list[dict[str, object]] = []
    authored_project = project / "authored"
    authored_project.mkdir()
    for args in (
        ("recipe", "init", "documented"),
        ("recipe", "compile", "documented"),
        ("recipe", "check", "documented"),
        ("recipe", "diff", "documented"),
        ("recipe", "render", "documented", "--view", "workflow"),
        ("recipe", "render", "documented", "--view", "generated"),
        ("recipe", "estimate", "documented", "--json"),
    ):
        result = subprocess.run(
            [str(executable), *args],
            cwd=authored_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if args[1] == "estimate":
            estimate = json.loads(result.stdout)
            assert estimate["schema"] == "lockstep.structural-estimate/v1"
            estimates.append(estimate)
    for template, name in (
        ("reviewed-change", "reviewed"),
        ("parallel-review", "parallel"),
    ):
        template_project = project / template
        template_project.mkdir()
        shown_result = subprocess.run(
            [str(executable), "template", "show", template, name],
            cwd=template_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert shown_result.returncode == 0, shown_result.stdout + shown_result.stderr
        shown = json.loads(shown_result.stdout)
        assert shown["template"] == template
        initialized = subprocess.run(
            [str(executable), "template", "init", template, name],
            cwd=template_project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert initialized.returncode == 0, initialized.stdout + initialized.stderr
        assert initialized.stdout == f"initialized {name}\n"
        for workflow in shown["compile_order"]:
            compiled = subprocess.run(
                [str(executable), "recipe", "compile", workflow],
                cwd=template_project,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        for args in (
            ("recipe", "check", name),
            ("recipe", "estimate", name, "--json"),
        ):
            result = subprocess.run(
                [str(executable), *args],
                cwd=template_project,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            if args[1] == "estimate":
                estimate = json.loads(result.stdout)
                assert estimate["schema"] == "lockstep.structural-estimate/v1"
                estimates.append(estimate)
    assert len(estimates) == 3
    return tuple(estimates)


def _assert_active_examples_compile(
    executable: Path, source_root: Path, project: Path, env: dict[str, str]
) -> tuple[dict[str, object], ...]:
    recipes = project / ".lockstep/recipes"
    recipes.mkdir(parents=True)
    examples = tuple(sorted((source_root / "recipes/examples").glob("*.recipe.yaml")))
    assert examples
    for example in examples:
        shutil.copy2(example, recipes / example.name)
    checked = subprocess.run(
        [str(executable), "recipe", "check", "--all"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    estimates: list[dict[str, object]] = []
    for example in examples:
        name = example.name.removesuffix(".recipe.yaml")
        result = subprocess.run(
            [str(executable), "recipe", "estimate", name, "--json"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        estimate = json.loads(result.stdout)
        assert estimate["schema"] == "lockstep.structural-estimate/v1"
        estimates.append(estimate)
    assert len(estimates) == len(examples)
    return tuple(estimates)


def _assert_surface_isolation(
    observed: dict[str, object],
    *,
    package_root: Path,
    environment_root: Path,
    exclude_checkout: bool,
) -> None:
    package_root = package_root.resolve(strict=True)
    environment_root = environment_root.resolve(strict=True)
    assert Path(str(observed["executable"])).is_relative_to(environment_root)
    module_paths = tuple(Path(item) for item in observed["loaded_modules"])
    assert module_paths and all(path.is_relative_to(package_root) for path in module_paths)
    for key in ("lockstep", "templates", "package_resource_root", "template_resource_root"):
        assert Path(str(observed[key])).is_relative_to(package_root)
    resource_files = tuple(
        Path(item)
        for key in ("package_resource_files", "template_resource_files")
        for item in observed[key]
    )
    assert resource_files and all(path.is_relative_to(package_root) for path in resource_files)
    assert any(path.name == "template.yaml" for path in resource_files)
    for key in ("distribution_root", "distribution_record"):
        assert Path(str(observed[key])).is_relative_to(environment_root)
    if exclude_checkout:
        leaked = [
            path
            for path in (*module_paths, *resource_files, *(Path(item) for item in observed["sys_path"]))
            if path.is_relative_to(ROOT)
        ]
        assert leaked == []


def _manual_estimate(observed: dict[str, object]) -> dict[str, object]:
    estimate = observed["manual_estimate"]
    assert isinstance(estimate, dict)
    assert estimate["schema"] == "lockstep.structural-estimate/v1"
    return estimate


def _assert_estimates_are_current(
    estimates: tuple[dict[str, object], ...],
) -> None:
    assert estimates
    assert all(
        estimate["schema"] == "lockstep.structural-estimate/v1"
        for estimate in estimates
    )
    assert all("peak_parallel_child_calls" in estimate for estimate in estimates)
    assert all("peak_parallel_subcalls" not in estimate for estimate in estimates)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel-dist")
    subprocess.run(["uv", "build", "--out-dir", str(output)], cwd=ENGINE, check=True)
    wheels = tuple(output.glob("lockstep-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_source_checkout_active_bytes_are_current() -> None:
    _assert_no_legacy_runner_importers(_active_file_bytes(ROOT, ACTIVE_ROOT_PATHS))
    _assert_active_bytes_are_retired(ROOT, ACTIVE_ROOT_PATHS)
    assert not (ENGINE / "src/lockstep/runtime/runners.py").exists()


@pytest.mark.parametrize(
    ("name", "source"),
    (
        ("lockstep/client.py", b"import lockstep.runtime.runners\n"),
        ("lockstep/client.py", b"from lockstep.runtime import runners\n"),
        ("lockstep/runtime/__init__.py", b"from . import runners\n"),
        ("lockstep/feature/client.py", b"from ..runtime import runners\n"),
        ("lockstep/runtime/client.py", b"from .runners import build_argv\n"),
    ),
)
def test_legacy_runner_import_oracle_catches_absolute_and_relative_imports(
    name: str, source: bytes
) -> None:
    with pytest.raises(AssertionError):
        _assert_no_legacy_runner_importers({name: source})


def test_source_checkout_active_guidance_describes_the_installed_contract() -> None:
    _assert_documented_authoring_grammar(ROOT)
    _assert_active_guidance(ROOT)


@pytest.mark.parametrize(
    "obsolete",
    (
        "`lockstep recipe init --template reviewed-change demo`",
        "- Run `lockstep recipe compile path/demo.workflow.yaml`.",
        "Inline `lockstep recipe estimate demo --format json` is stale.",
        "MCP: template_init",
    ),
)
def test_documented_grammar_oracle_rejects_inline_and_fenced_legacy_forms(
    tmp_path: Path, obsolete: str
) -> None:
    valid = "\n".join((*EXACT_DOCUMENTED_CLI, *EXACT_DOCUMENTED_MCP))
    paths = (
        "README.md",
        "docs/DESIGN.md",
        "skills/lockstep/SKILL.md",
        "skills/lockstep-author/SKILL.md",
    )
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(valid)
    _assert_documented_authoring_grammar(tmp_path)
    (tmp_path / "README.md").write_text(f"{valid}\n{obsolete}\n")
    with pytest.raises(AssertionError):
        _assert_documented_authoring_grammar(tmp_path)


def test_source_checkout_runs_all_complete_public_flows_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    source_environment = Path(sys.executable).absolute().parents[1]
    source_env = _clean_env(
        LOCKSTEP_STATE_DIR=str(tmp_path / "source-cli-state"),
        PATH=f"{source_environment / 'bin'}:/usr/bin:/bin",
    )
    observed = _run_probe(
        Path(sys.executable),
        tmp_path / "foreign-source",
        CONTROLLED_EFFECT,
        source_environment,
    )
    _assert_surface_isolation(
        observed,
        package_root=ENGINE / "src/lockstep",
        environment_root=source_environment,
        exclude_checkout=False,
    )
    cli_estimates = _assert_cli_resource_contract(
        source_environment / "bin/lockstep",
        tmp_path / "foreign-source-cli",
        source_env,
    )
    example_estimates = _assert_active_examples_compile(
        source_environment / "bin/lockstep",
        ROOT,
        tmp_path / "foreign-source-examples",
        source_env,
    )
    _assert_estimates_are_current(
        (*cli_estimates, *example_estimates, _manual_estimate(observed))
    )
    assert observed["legacy_runner_importable"] is False


def test_clean_wheel_isolated_install_contains_only_current_runtime_and_runs_full_flows(
    tmp_path: Path, built_wheel: Path
) -> None:
    venv = tmp_path / "wheel-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            str(built_wheel),
        ],
        cwd=tmp_path,
        env=_clean_env(PATH="/usr/local/bin:/usr/bin:/bin"),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            "pytest==9.1.1",
        ],
        cwd=tmp_path,
        env=_clean_env(PATH="/usr/local/bin:/usr/bin:/bin"),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(venv / "bin/lockstep-dependency-install")],
        cwd=tmp_path,
        env=_clean_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    controlled = tmp_path / "controlled-effect"
    shutil.copy2(CONTROLLED_EFFECT, controlled)
    controlled.chmod(controlled.stat().st_mode | stat.S_IXUSR)
    observed = _run_probe(python, tmp_path / "foreign-wheel", controlled, venv)
    _assert_surface_isolation(
        observed,
        package_root=Path(str(observed["lockstep"])).parent,
        environment_root=venv,
        exclude_checkout=True,
    )
    with zipfile.ZipFile(built_wheel) as archive:
        _assert_no_legacy_runner_importers(
            {
                name: archive.read(name)
                for name in archive.namelist()
                if name.endswith(".py")
            }
        )
    cli_estimates = _assert_cli_resource_contract(
        venv / "bin/lockstep",
        tmp_path / "foreign-wheel-cli",
        _clean_env(
            LOCKSTEP_STATE_DIR=str(tmp_path / "wheel-cli-state"),
            PATH=f"{venv / 'bin'}:/usr/bin:/bin",
        ),
    )
    _assert_estimates_are_current((*cli_estimates, _manual_estimate(observed)))
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
        assert not any(name.endswith("lockstep/runtime/runners.py") for name in names)
        for name in names:
            if name.endswith((".py", ".md", ".yaml", ".json")):
                content = archive.read(name)
                assert all(term not in content for term in RETIRED_BYTES), name
    assert observed["legacy_runner_importable"] is False


def _stage_plugin(destination: Path) -> None:
    tracked = _tracked()
    copied: set[str] = set()
    for relative in STAGED_DELIVERY_PATHS:
        source = ROOT / relative
        assert source.exists(), relative
        if source.is_dir():
            prefix = relative.rstrip("/") + "/"
            selected = sorted(path for path in tracked if path.startswith(prefix))
            assert selected, relative
            for path in selected:
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, target)
                copied.add(path)
        else:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.add(relative)
    assert copied <= tracked


def test_staged_delivery_excludes_source_examples_and_keeps_current_guidance(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "staged-plugin-guidance"
    _stage_plugin(stage)
    assert not (stage / "recipes").exists()
    _assert_documented_authoring_grammar(stage)
    _assert_active_guidance(stage)


def test_staged_plugin_uses_only_tracked_delivery_paths_and_runs_full_flows(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "staged-plugin"
    _stage_plugin(stage)
    foreign = tmp_path / "foreign-plugin"
    foreign.mkdir()
    doctor_state = tmp_path / "doctor-state"
    env = _clean_env(
        UV_OFFLINE="1",
        LOCKSTEP_STATE_DIR=str(doctor_state),
        PATH="/usr/local/bin:/usr/bin:/bin",
    )
    initialized = subprocess.run(
        [str(stage / "scripts/lockstep-plugin"), "recipe", "init", "doctor-probe"],
        cwd=foreign,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    env["PATH"] = f"{stage / 'engine/.venv/bin'}:/usr/local/bin:/usr/bin:/bin"
    doctor = subprocess.run(
        [str(stage / "scripts/lockstep-plugin"), "doctor"],
        cwd=foreign,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    server = subprocess.Popen(
        [str(stage / "scripts/lockstep-plugin"), "serve"],
        cwd=foreign,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gate-d", "version": "1"},
            },
        },
        separators=(",", ":"),
    )
    stdout, stderr = server.communicate(initialize + "\n", timeout=30)
    assert server.returncode == 0, stdout + stderr
    initialized = json.loads(stdout.splitlines()[-1])
    assert initialized["jsonrpc"] == "2.0"
    assert initialized["id"] == 1
    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert initialized["result"]["serverInfo"]["name"] == "lockstep"
    python = stage / "engine/.venv/bin/python"
    controlled = tmp_path / "staged-controlled-effect"
    shutil.copy2(CONTROLLED_EFFECT, controlled)
    controlled.chmod(controlled.stat().st_mode | stat.S_IXUSR)
    observed = _run_probe(
        python,
        tmp_path / "foreign-staged-flow",
        controlled,
        stage / "engine/.venv",
    )
    assert (
        json.loads((stage / ".codex-plugin/plugin.json").read_text())["name"]
        == "lockstep"
    )
    assert (
        json.loads((stage / ".claude-plugin/plugin.json").read_text())["name"]
        == "lockstep"
    )
    _assert_surface_isolation(
        observed,
        package_root=stage / "engine/src/lockstep",
        environment_root=stage / "engine/.venv",
        exclude_checkout=True,
    )
    _assert_no_legacy_runner_importers(
        _active_file_bytes(stage, ACTIVE_ROOT_PATHS)
    )
    cli_estimates = _assert_cli_resource_contract(
        stage / "engine/.venv/bin/lockstep", foreign / "resource-contract", env
    )
    _assert_estimates_are_current((*cli_estimates, _manual_estimate(observed)))
    _assert_active_bytes_are_retired(stage, ACTIVE_ROOT_PATHS)
    assert observed["legacy_runner_importable"] is False
