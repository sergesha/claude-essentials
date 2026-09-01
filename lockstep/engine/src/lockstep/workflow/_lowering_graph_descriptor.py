"""Protected graph-fragment descriptor rewriting."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.effects.descriptors import parse_effect_descriptor

from ._lowering_contracts import _FragmentNames
from .canonical import plain


def qualify_fragment_interrupt_channels(
    builder: Any,
    copied: dict[str, Any],
    names: _FragmentNames,
    fragment_state_keys: set[str],
) -> None:
    for field in ("state_key", "resume_key"):
        value = copied.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"fragment interrupt requires {field}")
        copied[field] = names.state_key(value)
        if copied[field] not in builder.state:
            builder.declare_generated_state(copied[field], "dict")
        fragment_state_keys.add(copied[field])


def protected_fragment_descriptor(
    copied: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    message = copied.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
    if not isinstance(descriptor, dict):
        raise ValueError("fragment interrupts must carry a protected descriptor")  # noqa: TRY004
    return message, plain(descriptor)


def _qualify_protected_descriptor(
    builder: Any,
    descriptor: dict[str, Any],
    names: _FragmentNames,
) -> None:
    if builder.inside_parallel_branch and descriptor.get("kind") == "decide":
        raise ValueError("parallel graph may not hide a decision descriptor")
    logical_id = descriptor.get("logical_id")
    if isinstance(logical_id, str):
        descriptor["logical_id"] = names.identity("effect", logical_id)
    builder._qualify_fragment_descriptor_state(descriptor, names)
    builder._qualify_fragment_descriptor_artifacts(descriptor, names)
    builder._inherit_fragment_scopes(descriptor)


def _rewrite_fragment_message(
    message: dict[str, Any],
    descriptor: dict[str, Any],
    names: _FragmentNames,
) -> None:
    if isinstance(message.get("step"), str):
        message["step"] = names.identity("step", message["step"])
    if message.get("artifact_contract") not in (None, [], {}):
        raise ValueError(
            "fragment artifact contracts must use protected descriptor artifacts"
        )
    for message_key, message_value in tuple(message.items()):
        if message_key != "lockstep_effect":
            message[message_key] = names.template(message_value)
    message["lockstep_effect"] = descriptor


def rewrite_fragment_descriptor(
    builder: Any,
    message: dict[str, Any],
    descriptor: dict[str, Any],
    names: _FragmentNames,
) -> Any:
    _qualify_protected_descriptor(builder, descriptor, names)
    _rewrite_fragment_message(message, descriptor, names)
    return parse_effect_descriptor(descriptor, known_state_keys=set(builder.state))
