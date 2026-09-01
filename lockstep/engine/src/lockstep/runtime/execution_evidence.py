"""Closed, passive public execution-evidence projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from lockstep.runtime._publication_queries import open_project_publication_queries
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    verify_owner_directory,
    verify_owner_file,
)
from lockstep.runtime.providers.base import PreparedLaunch, launch_commitment_digest
from lockstep.runtime.status import project_status

_EFFECT_KINDS = frozenset(
    {"managed", "manual", "pinned", "verify", "decide", "accept", "publish", "scope"}
)
_EFFECT_PHASES = frozenset(
    {"prepared", "launching", "running", "sealed", "indeterminate", "delivered"}
)
_SCOPE_PHASES = frozenset({"prepared", "sealed", "delivered"})


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _canonical_document(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not empty)
        or unicodedata.normalize("NFC", value) != value
        or any(
            (ord(char) < 0x20 and char not in {"\t", "\n"})
            or 0x7F <= ord(char) <= 0x9F
            for char in value
        )
        or len(value.encode()) > 4096
    ):
        raise ValueError(f"{label} is not bounded text")
    return value


def _digest(value: object, label: str) -> str:
    checked = _text(value, label)
    if len(checked) != 64 or any(char not in "0123456789abcdef" for char in checked):
        raise ValueError(f"{label} is not a lowercase SHA-256 digest")
    return checked


def _timestamp(value: object, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)",
            value,
        ) is None:
            raise ValueError(f"{label} is not an exact UTC timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{label} is not a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be UTC")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _coordinate(value: object, *, thread_id: str) -> dict[str, str]:
    if value.thread_id != thread_id:
        raise ValueError("effect coordinate thread does not match containing run")
    return {
        "checkpoint_id": _text(value.checkpoint_id, "checkpoint_id"),
        "checkpoint_ns": _text(value.checkpoint_ns, "checkpoint_ns", empty=True),
        "interrupt_id": _text(value.interrupt_id, "interrupt_id"),
        "task_id": _text(value.task_id, "task_id"),
        "thread_id": _text(value.thread_id, "thread_id"),
    }


def _relative_path(value: object, label: str) -> str:
    checked = _text(value, label)
    if len(checked.encode("utf-8")) > 512 or "\\" in checked:
        raise ValueError(f"{label} is not a safe relative path")
    raw_parts = checked.split("/")
    candidate = PurePosixPath(checked)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"{label} is not a safe relative path")
    return checked


def _acceptance_projection(effect: object) -> dict[str, object] | None:
    value = effect.result
    if value is None:
        return None
    required = {
        "schema",
        "effect_id",
        "outcome",
        "artifact_ref",
        "artifact_digest",
        "destination",
        "transformation",
        "audience",
        "consent_ref",
        "approval_generation",
        "receipt_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid acceptance result schema")
    generation = value["approval_generation"]
    if type(generation) is not int or not 1 <= generation <= 2_147_483_647:
        raise ValueError("invalid acceptance approval generation")
    projected = {
        "approval_generation": generation,
        "artifact_digest": _digest(value["artifact_digest"], "artifact_digest"),
        "artifact_ref": _text(value["artifact_ref"], "artifact_ref"),
        "audience": value["audience"],
        "consent_ref": _text(value["consent_ref"], "consent_ref"),
        "destination": _relative_path(value["destination"], "destination"),
        "effect_id": _text(value["effect_id"], "acceptance effect_id"),
        "outcome": value["outcome"],
        "receipt_digest": _digest(value["receipt_digest"], "receipt_digest"),
        "schema": value["schema"],
        "transformation": value["transformation"],
    }
    if (
        projected["schema"] != "lockstep.acceptance-result/v1"
        or projected["effect_id"] != effect.effect_id
        or projected["outcome"] != "PASS"
        or projected["audience"] != "local-project"
        or projected["transformation"] != "identity"
    ):
        raise ValueError("acceptance result does not match its effect")
    return projected


def _read_owner_json(path: Path, *, limit: int = 2 * 1024 * 1024) -> tuple[dict[str, object], bytes]:
    verify_owner_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("owner JSON file is not a bounded regular file")
        encoded = bytearray()
        while len(encoded) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(encoded)))
            if not chunk:
                break
            encoded.extend(chunk)
    finally:
        os.close(descriptor)
    if len(encoded) > limit:
        raise ValueError("owner JSON file exceeds public read limit")
    try:
        value = json.loads(bytes(encoded).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("owner JSON file is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("owner JSON file must contain an object")
    return value, bytes(encoded)


def _ref256(value: object, label: str) -> str:
    checked = _text(value, label)
    if re.fullmatch(r"[0-9a-f]{64}", checked) is None:
        raise ValueError(f"{label} is not a 256-bit public reference")
    return checked


def _safe_start_value(effect_id: str, public_launch_ref: str, start_ref: str) -> dict[str, object]:
    return {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": effect_id,
        "public_launch_ref": public_launch_ref,
        "start_ref": start_ref,
    }


def _safe_record_state(path: Path, expected: dict[str, object]) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        verify_owner_file(path)
        value, encoded = _read_owner_json(path, limit=64 * 1024)
    except (InsecureStatePath, OSError, ValueError):
        return "invalid"
    if value != expected or encoded != _canonical_document(expected):
        return "invalid"
    return "valid"


def _terminal_projection(
    path: Path,
    *,
    effect_id: str,
    public_launch_ref: str,
    start_ref: str,
) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    value, encoded = _read_owner_json(path, limit=64 * 1024)
    required = {
        "effect_id",
        "overflow",
        "public_launch_ref",
        "quiescent",
        "returncode",
        "start_ref",
        "terminal_ref",
        "termination_reason",
        "timed_out",
    }
    if set(value) != required or encoded != _canonical_document(value):
        raise ValueError("invalid public terminal schema")
    if (
        value["effect_id"] != effect_id
        or value["public_launch_ref"] != public_launch_ref
        or value["start_ref"] != start_ref
        or type(value["returncode"]) is not int
        or not -(2**31) <= value["returncode"] < 2**31
        or any(type(value[key]) is not bool for key in ("overflow", "quiescent", "timed_out"))
        or value["termination_reason"]
        not in {
            "exited",
            "cancelled",
            "deadline",
            "output_overflow",
            "spawn_failed",
            "stdin_failed",
            "receipt_publication_failed",
        }
    ):
        raise ValueError("public terminal does not match its launch")
    safe = {key: value[key] for key in required if key != "terminal_ref"}
    expected_ref = hashlib.sha256(
        b"lockstep-public-terminal-v1\0" + _canonical(safe)
    ).hexdigest()
    if value["terminal_ref"] != expected_ref:
        raise ValueError("public terminal digest mismatch")
    return {key: value[key] for key in sorted(required)}


def _attempt_launch_record(
    effect: object, state_dir: Path
) -> tuple[Path, dict[str, object], bytes] | None:
    attempts = state_dir / "codex-attempts"
    if not attempts.exists() and not attempts.is_symlink():
        return None
    verify_owner_directory(attempts)
    directory = attempts / hashlib.sha256(effect.effect_id.encode()).hexdigest()
    if not directory.exists() and not directory.is_symlink():
        return None
    verify_owner_directory(directory)
    launch_path = directory / "launch.json"
    if not launch_path.exists() and not launch_path.is_symlink():
        if any(directory.iterdir()):
            raise ValueError("partial Codex attempt has no bound launch record")
        return None
    raw, encoded = _read_owner_json(launch_path)
    return directory, raw, encoded


def _validate_private_launch(
    effect: object,
    raw: dict[str, object],
    encoded: bytes,
    *,
    verify_ledger_commitment: bool,
) -> tuple[str, str, str, str]:
    required = {
        "schema", "effect_id", "request_digest", "runner_binding_digest",
        "workspace_ref", "workspace_path", "workspace_purpose", "execution_class",
        "cwd", "executable_path", "executable_identity_digest", "inner_argv",
        "environment", "codex_home", "credential_identity_digest",
        "sandbox_policy_digest", "sandbox_attestation_digest",
        "launcher_decision_generation", "deadline_at", "launch_ref",
        "public_launch_ref", "start_ref", "shell", "close_fds", "inherited_fds",
        "deployment_profile",
    }
    if set(raw) != required or encoded != _canonical(raw):
        raise ValueError("invalid private Codex launch schema")
    workspace_path = _text(raw["workspace_path"], "workspace_path")
    executable = _text(raw["executable_path"], "executable_path")
    _validate_private_launch_identity(
        effect, raw, workspace_path=workspace_path, executable=executable
    )
    if verify_ledger_commitment:
        prepared = PreparedLaunch(
            effect.effect_id,
            effect.request_digest,
            effect.runner_binding_digest,
            _text(raw["launch_ref"], "private launch_ref"),
            effect.workspace_ref,
        )
        if (
            effect.grant_digest is None
            or effect.launch_commitment_digest is None
            or launch_commitment_digest(effect, prepared)
            != effect.launch_commitment_digest
        ):
            raise ValueError("private Codex launch commitment does not join its effect")
    public_launch_ref = _ref256(raw["public_launch_ref"], "public_launch_ref")
    start_ref = _ref256(raw["start_ref"], "start_ref")
    if public_launch_ref == start_ref:
        raise ValueError("public launch references must be independent")
    return workspace_path, executable, public_launch_ref, start_ref


def _validate_private_launch_identity(
    effect: object,
    raw: dict[str, object],
    *,
    workspace_path: str,
    executable: str,
) -> None:
    if (
        raw["schema"] != "lockstep.codex-launch/v1"
        or raw["effect_id"] != effect.effect_id
        or raw["request_digest"] != effect.request_digest
        or raw["runner_binding_digest"] != effect.runner_binding_digest
        or raw["workspace_ref"] != effect.workspace_ref
        or raw["execution_class"] != "managed-agent"
        or raw["workspace_purpose"] != "managed_output"
        or raw["cwd"] != workspace_path
        or not Path(workspace_path).is_absolute()
        or not Path(executable).is_absolute()
        or raw["shell"] is not False
        or raw["close_fds"] is not True
        or raw["inherited_fds"] != []
        or raw["deployment_profile"] != "local_unsandboxed"
    ):
        raise ValueError("private Codex launch does not join its effect")


def _normalized_managed_argv(
    raw: dict[str, object], *, executable: str, workspace_path: str
) -> tuple[list[str], str]:
    argv = raw["inner_argv"]
    if (
        not isinstance(argv, list)
        or len(argv) != 15
        or any(not isinstance(item, str) or not item for item in argv)
        or argv[0] != executable
        or argv[1:8]
        != ["--ask-for-approval", "never", "exec", "--json", "--sandbox", "workspace-write", "--model"]
        or argv[9:13]
        != ["--ephemeral", "--ignore-user-config", "--ignore-rules", "-C"]
        or argv[13] != workspace_path
        or argv[14] != "-"
        or argv.count("-C") != 1
    ):
        raise ValueError("private Codex argv is not the managed profile")
    normalized = list(argv)
    normalized[13] = "$LOCKSTEP_EFFECT_WORKSPACE"
    normalized_ref = hashlib.sha256(
        b"lockstep-public-managed-argv-v1\0" + _canonical(normalized)
    ).hexdigest()
    return normalized, normalized_ref


def _spawn_projection(
    directory: Path,
    *,
    effect_id: str,
    public_launch_ref: str,
    start_ref: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    safe_start = _safe_start_value(effect_id, public_launch_ref, start_ref)
    fence = _safe_record_state(directory / "spawn-fence.json", safe_start)
    started = _safe_record_state(directory / "public-start.json", safe_start)
    terminal_path = directory / "public-terminal.json"
    if fence == "absent":
        if started != "absent" or terminal_path.exists() or terminal_path.is_symlink():
            raise ValueError("public start or terminal exists without a fence")
        spawn = None
        terminal = None
    else:
        if fence != "valid":
            raise ValueError("invalid public spawn fence")
        disposition = "started" if started == "valid" else "indeterminate"
        spawn = {
            "disposition": disposition,
            "effect_id": effect_id,
            "process_start_count": 1 if disposition == "started" else None,
            "public_launch_ref": public_launch_ref,
            "start_ref": start_ref,
        }
        terminal = _terminal_projection(
            terminal_path,
            effect_id=effect_id,
            public_launch_ref=public_launch_ref,
            start_ref=start_ref,
        )
    return spawn, terminal


def _launch_projection(
    effect: object, *, state_dir: Path, verify_ledger_commitment: bool = True
) -> dict[str, object] | None:
    observed = _attempt_launch_record(effect, state_dir)
    if observed is None:
        return None
    directory, raw, encoded = observed
    workspace_path, executable, public_launch_ref, start_ref = _validate_private_launch(
        effect, raw, encoded, verify_ledger_commitment=verify_ledger_commitment
    )
    normalized, normalized_ref = _normalized_managed_argv(
        raw, executable=executable, workspace_path=workspace_path
    )
    spawn, terminal = _spawn_projection(
        directory,
        effect_id=effect.effect_id,
        public_launch_ref=public_launch_ref,
        start_ref=start_ref,
    )
    return {
        "execution_class": "managed-codex",
        "normalized_argv": normalized,
        "normalized_argv_ref": normalized_ref,
        "public_launch_ref": public_launch_ref,
        "resolved_executable": executable,
        "spawn": spawn,
        "terminal": terminal,
    }


def _validated_publication_journal(
    effect: object, binding: object, state_dir: Path
) -> tuple[str, str, dict[str, object], str]:
    prefix = "publication:"
    if not isinstance(effect.result_ref, str) or not effect.result_ref.startswith(prefix):
        raise ValueError("publish effect has an invalid result reference")
    journal_digest = _digest(effect.result_ref.removeprefix(prefix), "journal digest")
    queries = open_project_publication_queries(state_dir, binding.project_identity)
    publisher_digest = queries.binding_digest
    handle, journal = queries.validated_journal_digest(journal_digest)
    required_journal = {
        "schema", "phase", "request_digest", "publisher_binding_digest",
        "request", "plan", "cursor",
    }
    if set(journal) != required_journal or journal["schema"] != "lockstep.publication-journal/v1":
        raise ValueError("invalid publication journal schema")
    if journal["phase"] not in {
        "prepared", "applying", "rollback_pending", "applied", "rolled_back"
    }:
        raise ValueError("invalid publication journal phase")
    if (
        journal["publisher_binding_digest"] != publisher_digest
        or not isinstance(journal["plan"], list)
        or type(journal["cursor"]) is not int
    ):
        raise ValueError("publication journal does not join its effect")
    if (
        effect.runner_binding_digest != publisher_digest
        or effect.launch_commitment_digest != queries.commitment_digest(handle)
    ):
        raise ValueError("native publication commitment does not join its effect")
    return journal_digest, handle.request_digest, journal, publisher_digest


def _validated_publication_request(
    effect: object,
    binding: object,
    journal_digest: str,
    publication_request_digest: str,
    journal: dict[str, object],
    publisher_digest: str,
) -> tuple[dict[str, object], list[object], list[object]]:
    request = journal["request"]
    _validate_publication_request_joins(
        effect,
        binding,
        request,
        publication_request_digest=publication_request_digest,
        publisher_digest=publisher_digest,
    )
    expected_journal = hashlib.sha256(
        _canonical(
            {
                "schema": "lockstep.publication-journal-key/v1",
                "request_digest": publication_request_digest,
                "publisher_binding_digest": publisher_digest,
            }
        )
    ).hexdigest()
    if expected_journal != journal_digest:
        raise ValueError("publication journal key digest mismatch")
    entries = request["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("publication entries must contain from 1 to 32 items")
    plan = journal["plan"]
    if len(plan) != len(entries):
        raise ValueError("publication plan does not join request cardinality")
    return request, entries, plan


def _validate_publication_request_joins(
    effect: object,
    binding: object,
    request: object,
    *,
    publication_request_digest: str,
    publisher_digest: str,
) -> None:
    required_request = {
        "schema", "effect_id", "public_run_id", "project_identity",
        "definition_digest", "coordinate", "descriptor_digest",
        "authority_request_digest", "grant_digest", "publisher_binding_digest",
        "consent_ref", "approval_generation", "policy_epoch", "config_epoch",
        "parent_capability_generation", "entries",
    }
    if (
        not isinstance(request, dict)
        or set(request) != required_request
        or request["schema"] != "lockstep.publication-request/v1"
        or hashlib.sha256(_canonical(request)).hexdigest()
        != publication_request_digest
        or request["effect_id"] != effect.effect_id
        or request["public_run_id"] != binding.public_run_id
        or request["project_identity"] != binding.project_identity
        or request["definition_digest"] != binding.recipe_digest
        or request["descriptor_digest"] != effect.descriptor_digest
        or request["authority_request_digest"] != effect.request_digest
        or request["publisher_binding_digest"] != publisher_digest
        or request["coordinate"] != _coordinate(effect.coordinate, thread_id=binding.thread_id)
    ):
        raise ValueError("publication request does not join its effect")


def _publication_consent_index(
    consents: tuple[object, ...],
) -> dict[tuple[str, str, str], list[object]]:
    consent_by_entry: dict[tuple[str, str, str], list[object]] = {}
    for consent in consents:
        key = (consent.artifact_ref, consent.destination, consent.transformation)
        consent_by_entry.setdefault(key, []).append(consent)
    return consent_by_entry


def _matched_publication_consent(
    entry: object,
    planned: object,
    *,
    binding: object,
    consent_by_entry: dict[tuple[str, str, str], list[object]],
) -> object:
    if not isinstance(entry, dict) or set(entry) != {
        "artifact_ref", "destination", "transformation"
    }:
        raise ValueError("invalid publication entry schema")
    key = (
        _text(entry["artifact_ref"], "publication artifact_ref"),
        _relative_path(entry["destination"], "publication destination"),
        entry["transformation"],
    )
    if (
        not isinstance(planned, dict)
        or planned.get("artifact_ref") != key[0]
        or planned.get("destination") != key[1]
        or planned.get("transformation") != key[2]
    ):
        raise ValueError("publication plan does not join request order")
    matches = consent_by_entry.get(key, [])
    if len(matches) != 1:
        raise ValueError("publication entry has no unique accepted consent")
    consent = matches[0]
    if (
        consent.transformation != "identity"
        or consent.audience != "local-project"
        or consent.public_run_id != binding.public_run_id
        or consent.project_identity != binding.project_identity
        or consent.definition_digest != binding.recipe_digest
        or consent.redeemed_at is None
        or consent.receipt_digest is None
    ):
        raise ValueError("publication consent is not a redeemed current commitment")
    return consent


def _publication_consent_commitment(
    consent: object, binding: object, planned: dict[str, object]
) -> dict[str, object]:
    commitment = {
            "artifact_digest": _digest(consent.artifact_digest, "artifact digest"),
            "artifact_ref": _text(consent.artifact_ref, "artifact_ref"),
            "audience": consent.audience,
            "definition_digest": _digest(consent.definition_digest, "definition digest"),
            "descriptor_digest": _digest(consent.descriptor_digest, "descriptor digest"),
            "destination": _relative_path(consent.destination, "destination"),
            "effect_id": _text(consent.effect_id, "consent effect_id"),
            "producer_effect_id": _text(consent.producer_effect_id, "producer effect_id"),
            "project_identity": consent.project_identity,
            "public_run_id": consent.public_run_id,
            "schema": "lockstep.publication-consent-commitment/v1",
            "source": _coordinate(consent.source, thread_id=binding.thread_id),
            "transformation": consent.transformation,
    }
    commitment_digest = hashlib.sha256(_canonical(commitment)).hexdigest()
    after = planned.get("after")
    if commitment_digest != consent.commitment_digest:
        raise ValueError("publication consent commitment digest mismatch")
    if not isinstance(after, dict) or after.get("sha256") != consent.artifact_digest:
        raise ValueError("publication plan image does not join accepted artifact")
    commitment["digest"] = commitment_digest
    return commitment


def _validate_publication_ancestry(
    consent: object, effects_by_id: dict[str, object]
) -> None:
    accepted_effect = effects_by_id.get(consent.effect_id)
    producer_effect = effects_by_id.get(consent.producer_effect_id)
    accepted = (
        None
        if accepted_effect is None or accepted_effect.effect_kind != "accept"
        else _acceptance_projection(accepted_effect)
    )
    if (
        accepted is None
        or producer_effect is None
        or accepted_effect.coordinate != consent.source
        or accepted_effect.descriptor_digest != consent.descriptor_digest
        or accepted["receipt_digest"] != consent.receipt_digest
        or accepted["consent_ref"] != consent.consent_ref
        or accepted["artifact_ref"] != consent.artifact_ref
        or accepted["artifact_digest"] != consent.artifact_digest
        or accepted["destination"] != consent.destination
    ):
        raise ValueError("publication item does not join its accepted ancestor")


def _publication_item(
    ordinal: int,
    entry: object,
    planned: object,
    *,
    binding: object,
    consent_by_entry: dict[tuple[str, str, str], list[object]],
    effects_by_id: dict[str, object],
) -> dict[str, object]:
    consent = _matched_publication_consent(
        entry, planned, binding=binding, consent_by_entry=consent_by_entry
    )
    assert isinstance(planned, dict)
    commitment = _publication_consent_commitment(consent, binding, planned)
    _validate_publication_ancestry(consent, effects_by_id)
    return {
        "acceptance_receipt_digest": _digest(
            consent.receipt_digest, "acceptance receipt digest"
        ),
        "commitment": commitment,
        "consent_ref": _text(consent.consent_ref, "consent_ref"),
        "ordinal": ordinal,
    }


def _validate_publication_items(
    request: dict[str, object], projected_items: list[dict[str, object]]
) -> None:
    consent_refs = [item["consent_ref"] for item in projected_items]
    expected_set = "consent-set:" + hashlib.sha256(
        json.dumps(consent_refs, separators=(",", ":")).encode()
    ).hexdigest()
    if request["consent_ref"] != expected_set:
        raise ValueError("publication consent set does not match item order")
    for key in ("consent_ref", "acceptance_receipt_digest"):
        if len({item[key] for item in projected_items}) != len(projected_items):
            raise ValueError("publication items contain a duplicate safe identity")
    if len({item["commitment"]["artifact_ref"] for item in projected_items}) != len(projected_items):
        raise ValueError("publication items contain duplicate artifacts")
    if len({item["commitment"]["destination"] for item in projected_items}) != len(projected_items):
        raise ValueError("publication items contain duplicate destinations")


def _publication_projection(
    effect: object,
    *,
    binding: object,
    state_dir: Path,
    consents: tuple[object, ...],
    effects_by_id: dict[str, object],
) -> dict[str, object] | None:
    if effect.result_ref is None:
        return None
    (
        journal_digest,
        publication_request_digest,
        journal,
        publisher_digest,
    ) = _validated_publication_journal(
        effect, binding, state_dir
    )
    request, entries, plan = _validated_publication_request(
        effect,
        binding,
        journal_digest,
        publication_request_digest,
        journal,
        publisher_digest,
    )
    consent_by_entry = _publication_consent_index(consents)
    projected_items = [
        _publication_item(
            ordinal,
            entry,
            plan[ordinal],
            binding=binding,
            consent_by_entry=consent_by_entry,
            effects_by_id=effects_by_id,
        )
        for ordinal, entry in enumerate(entries)
    ]
    _validate_publication_items(request, projected_items)
    return {
        "effect_id": effect.effect_id,
        "items": projected_items,
        "journal_digest": journal_digest,
        "phase": journal["phase"],
    }


def _effect_projection(
    effect: object,
    *,
    binding: object,
    state_dir: Path,
    consents: tuple[object, ...],
    effects_by_id: dict[str, object],
) -> dict[str, object]:
    kind = effect.effect_kind
    phase = effect.phase
    if kind not in _EFFECT_KINDS:
        raise ValueError("unknown durable effect kind")
    if phase not in _EFFECT_PHASES:
        raise ValueError("unknown durable effect phase")
    if kind == "scope" and phase not in _SCOPE_PHASES:
        raise ValueError("scope effect has a launch-only phase")
    acceptance = _acceptance_projection(effect) if kind == "accept" else None
    launch = _launch_projection(effect, state_dir=state_dir) if kind == "managed" else None
    publication = (
        _publication_projection(
            effect,
            binding=binding,
            state_dir=state_dir,
            consents=consents,
            effects_by_id=effects_by_id,
        )
        if kind == "publish"
        else None
    )
    return {
        "acceptance": acceptance,
        "coordinate": _coordinate(effect.coordinate, thread_id=binding.thread_id),
        "descriptor_digest": _digest(effect.descriptor_digest, "descriptor_digest"),
        "effect_id": _text(effect.effect_id, "effect_id"),
        "effect_kind": kind,
        "launch": launch,
        "phase": phase,
        "publication": publication,
        "updated_at": _timestamp(effect.updated_at, "effect updated_at"),
    }


def _run_projection(resources: object, binding: object, *, state_dir: Path) -> dict[str, object]:
    effects = resources.effects_for_thread(binding.thread_id)
    projected_effects = effects.list_for_thread(binding.thread_id)
    effects_by_id = {effect.effect_id: effect for effect in projected_effects}
    consents = resources.publication_consents_for_run(
        binding.public_run_id, binding.project_identity
    )
    with resources.native_app(binding) as app:
        snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
        if projected_effects and not snapshot.checkpoint_id:
            raise ValueError("durable effects require a retained current checkpoint")
        for effect in projected_effects:
            coordinate = effect.coordinate
            if (
                coordinate.checkpoint_id == snapshot.checkpoint_id
                and coordinate.checkpoint_ns == snapshot.checkpoint_ns
            ):
                continue
            if not app.checkpoint_is_ancestor(
                thread_id=binding.thread_id,
                ancestor_checkpoint_ns=coordinate.checkpoint_ns,
                ancestor_checkpoint_id=coordinate.checkpoint_id,
                descendant_checkpoint_ns=snapshot.checkpoint_ns,
                descendant_checkpoint_id=snapshot.checkpoint_id,
                snapshot_limit=1024,
            ):
                raise ValueError("effect coordinate is not an ancestor of current checkpoint")
    ordered = sorted(
        (
            _effect_projection(
                effect,
                binding=binding,
                state_dir=state_dir,
                consents=consents,
                effects_by_id=effects_by_id,
            )
            for effect in projected_effects
        ),
        key=lambda item: (
            item["coordinate"]["thread_id"],
            item["coordinate"]["checkpoint_ns"],
            item["coordinate"]["checkpoint_id"],
            item["coordinate"]["task_id"],
            item["coordinate"]["interrupt_id"],
            item["effect_id"],
        ),
    )
    if len({item["effect_id"] for item in ordered}) != len(ordered):
        raise ValueError("duplicate effect identity")
    status = project_status(binding, snapshot, (), effects).status
    checkpoint = None
    if snapshot.checkpoint_id or snapshot.checkpoint_ns:
        checkpoint = {
            "checkpoint_id": _text(
                snapshot.checkpoint_id, "current checkpoint_id", empty=True
            ),
            "checkpoint_ns": _text(
                snapshot.checkpoint_ns, "current checkpoint_ns", empty=True
            ),
            "created_at": (
                None
                if snapshot.created_at is None
                else _timestamp(snapshot.created_at, "checkpoint created_at")
            ),
            "status": status,
        }
    public_run_id = _text(binding.public_run_id, "public_run_id")
    if re.search(r"-[0-9a-f]{32}$", public_run_id) is None:
        raise ValueError("public_run_id does not end in its public random suffix")
    return {
        "checkpoint": checkpoint,
        "definition_digest": _digest(binding.recipe_digest, "definition_digest"),
        "effects": ordered,
        "public_run_id": public_run_id,
        "snapshot_ref": _text(binding.recipe_snapshot_ref, "snapshot_ref"),
        "thread_id": _text(binding.thread_id, "thread_id"),
    }


def _unmatched_launches(
    resources: object,
    *,
    state_dir: Path,
    runs: list[dict[str, object]],
) -> list[dict[str, object]]:
    attempts = state_dir / "codex-attempts"
    if not attempts.exists() and not attempts.is_symlink():
        return []
    verify_owner_directory(attempts)
    included_runs = {run["public_run_id"] for run in runs}
    ledger_effects = {
        effect["effect_id"] for run in runs for effect in run["effects"]
    }
    input_bindings = resources.effect_input_run_bindings()
    result: list[dict[str, object]] = []
    for index, directory in enumerate(sorted(attempts.iterdir(), key=lambda path: path.name)):
        if index >= 10_000:
            raise ValueError("Codex attempt scan exceeds public bound")
        verify_owner_directory(directory)
        launch_path = directory / "launch.json"
        if not launch_path.exists() and not launch_path.is_symlink():
            continue
        raw, _encoded = _read_owner_json(launch_path)
        effect_id = _text(raw.get("effect_id"), "attempt effect_id")
        if directory.name != hashlib.sha256(effect_id.encode()).hexdigest():
            raise ValueError("Codex attempt directory does not match its effect")
        if effect_id in ledger_effects or input_bindings.get(effect_id) not in included_runs:
            continue
        pseudo_effect = SimpleNamespace(
            effect_id=effect_id,
            request_digest=raw.get("request_digest"),
            runner_binding_digest=raw.get("runner_binding_digest"),
            workspace_ref=raw.get("workspace_ref"),
            launch_commitment_digest=raw.get("launch_ref"),
        )
        launch = _launch_projection(
            pseudo_effect, state_dir=state_dir, verify_ledger_commitment=False
        )
        if launch is None:
            raise ValueError("bound unmatched attempt has no launch projection")
        spawn = launch["spawn"]
        result.append(
            {
                "disposition": (
                    "indeterminate" if spawn is None else spawn["disposition"]
                ),
                "effect_id": effect_id,
                "normalized_argv_ref": launch["normalized_argv_ref"],
                "process_start_count": (
                    None if spawn is None else spawn["process_start_count"]
                ),
                "public_launch_ref": launch["public_launch_ref"],
                "start_ref": None if spawn is None else spawn["start_ref"],
            }
        )
    return sorted(result, key=lambda item: (item["effect_id"], item["public_launch_ref"]))


def project_execution_evidence(
    resources: object,
    *,
    state_dir: Path,
    project_identity: str,
    selected_run_id: str | None,
    bindings: tuple[object, ...],
) -> dict[str, object]:
    """Project verified existing facts without initializing owner state."""

    project_path = Path(_text(project_identity, "project_identity"))
    if not project_path.is_absolute() or str(project_path) != str(project_path.resolve()):
        raise ValueError("project_identity is not a canonical absolute path")
    if len(bindings) > 10_000:
        raise ValueError("execution evidence run limit exceeded")
    if resources.effect_count_for_threads(
        tuple(binding.thread_id for binding in bindings), limit=10_000
    ) > 10_000:
        raise ValueError("execution evidence effect limit exceeded")
    runs = sorted(
        (_run_projection(resources, binding, state_dir=state_dir) for binding in bindings),
        key=lambda item: item["public_run_id"],
    )
    if sum(len(item["effects"]) for item in runs) > 10_000:
        raise ValueError("execution evidence effect limit exceeded")
    if selected_run_id is not None and (
        len(runs) != 1 or runs[0]["public_run_id"] != selected_run_id
    ):
        raise ValueError("selected execution evidence run mismatch")
    unmatched = _unmatched_launches(
        resources, state_dir=state_dir, runs=runs
    )
    value: dict[str, object] = {
        "schema": "lockstep.execution-evidence/v1",
        "project_identity": project_identity,
        "selected_run_id": selected_run_id,
        "runs": runs,
        "unmatched_launches": unmatched,
    }
    value["projection_digest"] = hashlib.sha256(_canonical(value)).hexdigest()
    return value
