"""Exact child artifact producer matching."""

from __future__ import annotations

from typing import Any

import yaml


def _artifact_producer_candidate(
    source_file: Any,
    node: Any,
    qualified: str,
    spec: tuple[str, str, str, str, str],
) -> tuple[str, tuple[str, str, str, str, str, str]] | None:
    if not isinstance(node, dict) or node.get("type") != "interrupt":
        return None
    message = node.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
    resume_key = node.get("resume_key")
    if not isinstance(descriptor, dict) or not isinstance(resume_key, str):
        return None
    declared_name, source, media_type, producer_logical_id, result_key = spec
    if descriptor.get("logical_id") != producer_logical_id or resume_key != result_key:
        return None
    declarations = descriptor.get("artifacts")
    expected = {
        "name": declared_name,
        "source_path": source,
        "media_type": media_type,
        "required": True,
    }
    artifact_contract = message.get("artifact_contract")
    metadata_matches = (
        isinstance(artifact_contract, dict)
        and artifact_contract.get("handle") == declared_name
        and artifact_contract.get("path") == source
        and isinstance(artifact_contract.get("markdown"), dict)
        and isinstance(artifact_contract["markdown"].get("sections"), list)
    )
    declarations_match = (
        isinstance(declarations, list)
        and sum(item == expected for item in declarations) == 1
    )
    if not declarations_match and not (declarations == [] and metadata_matches):
        raise ValueError("child artifact contract differs from producer declaration")
    return source_file.relative_path, (
        qualified,
        declared_name,
        source,
        media_type,
        resume_key,
        producer_logical_id,
    )


def artifact_producer_candidates(
    resolved: Any,
    qualified: str,
    spec: tuple[str, str, str, str, str],
) -> list[tuple[str, tuple[str, str, str, str, str, str]]]:
    candidates: list[tuple[str, tuple[str, str, str, str, str, str]]] = []
    for source_file in resolved.standalone.files:
        document = yaml.safe_load(source_file.content)
        nodes = document.get("nodes", {}) if isinstance(document, dict) else {}
        for node in nodes.values() if isinstance(nodes, dict) else ():
            candidate = _artifact_producer_candidate(source_file, node, qualified, spec)
            if candidate is not None:
                candidates.append(candidate)
    return candidates
