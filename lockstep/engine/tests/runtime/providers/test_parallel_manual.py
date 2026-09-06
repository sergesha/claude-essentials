"""Aggregate manual manifests admit siblings but retain whole-project checks."""

from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission
from tests.runtime.providers.test_manual import _manual_descriptor


def _prepare(tmp_path: Path):
    owner, project = tmp_path / "owner", tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "docs").mkdir()
    raw = _manual_descriptor()
    raw["parallel"] = {"id": "work", "branch": "code", "writes": ["src/", "docs/"]}
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    provider = ManualProvider(owner, BlobStore(owner))
    handoff = provider.prepare_handoff(
        binding,
        NativeInterrupt(coordinate, {"lockstep_effect": raw}),
        parse_effect_descriptor(raw),
    )
    return provider, handoff, project


@pytest.mark.parametrize(
    "outcome,expected", [("PASS", "PASS"), ("FAIL", "FAIL"), ("ABORTED", "ERROR")]
)
def test_parallel_manual_restarted_handoff_admits_completed_sibling_writes(
    tmp_path: Path, outcome: str, expected: str
) -> None:
    _provider, handoff, project = _prepare(tmp_path)
    (project / "docs/readme.md").write_text("sibling completed")
    (project / "src/app.py").write_text("own edit")
    restarted = ManualProvider(tmp_path / "owner", BlobStore(tmp_path / "owner"))
    restored = restarted.lookup(handoff.effect_id)
    assert restored == handoff
    assert restored.writes == ("src/",)
    result = restarted.submit(
        restored,
        ManualSubmission.build(
            outcome, reason="blocked" if outcome == "FAIL" else None
        ),
    )
    assert result.outcome == expected
    assert result.fixed_error_code == ("cancelled" if outcome == "ABORTED" else None)


@pytest.mark.parametrize("outcome", ["PASS", "FAIL", "ABORTED"])
@pytest.mark.parametrize("mutation", ["outside", "symlink", "symlink_surface", "git"])
def test_parallel_manual_retains_integrity_checks_on_every_outcome(
    tmp_path: Path, outcome: str, mutation: str
) -> None:
    provider, handoff, project = _prepare(tmp_path)
    (project / "docs/readme.md").write_text("allowed sibling edit")
    if mutation == "outside":
        (project / "outside.txt").write_text("undeclared")
    elif mutation == "symlink":
        (project / "docs/link").symlink_to("readme.md")
    elif mutation == "symlink_surface":
        (project / "src").rmdir()
        (project / "src").symlink_to("docs", target_is_directory=True)
    else:
        (project / ".git").mkdir()
        (project / ".git/HEAD").write_text("ref: refs/heads/main\n")
    submission = ManualSubmission.build(
        outcome, reason="blocked" if outcome == "FAIL" else None
    )
    result = provider.submit(handoff, submission)
    assert result.outcome == "ERROR"
    assert result.fixed_error_code == "manifest_invalid"


def test_parallel_handoff_cannot_be_rebound_to_a_larger_surface(tmp_path: Path) -> None:
    from dataclasses import replace

    from lockstep.runtime.providers.manual import ManualProviderError

    provider, handoff, _ = _prepare(tmp_path)
    changed = replace(
        handoff,
        parallel=replace(handoff.parallel, writes=("src/", "docs/", "outside.txt")),
    )
    with pytest.raises(ManualProviderError, match="another handoff"):
        provider.submit(changed, ManualSubmission.build("PASS"))


@pytest.mark.parametrize(
    "contract",
    [
        {"id": "work", "branch": "code", "writes": ["docs/"]},
        {"id": "work", "branch": "code", "writes": ["src/", "../outside"]},
        {"id": "work", "branch": "code", "writes": ["src/", "src/"]},
        {"id": "work", "branch": "code", "writes": ["src/"], "extra": True},
    ],
)
def test_parallel_descriptor_rejects_invalid_aggregate_contract(contract: dict) -> None:
    raw = _manual_descriptor()
    raw["parallel"] = contract
    with pytest.raises(ValueError):
        parse_effect_descriptor(raw)


def test_parallel_aggregate_is_part_of_descriptor_digest() -> None:
    raw = _manual_descriptor()
    raw["parallel"] = {"id": "work", "branch": "code", "writes": ["src/", "docs/"]}
    first = parse_effect_descriptor(raw)
    assert parse_effect_descriptor(first.to_dict()) == first
    raw["parallel"] = {"id": "work", "branch": "code", "writes": ["src/", "other/"]}
    assert parse_effect_descriptor(raw).digest != first.digest
