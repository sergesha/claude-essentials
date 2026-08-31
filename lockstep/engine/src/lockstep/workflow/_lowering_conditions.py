"""Parsing and conservative evaluation of yamlgraph conditions."""

from __future__ import annotations

import re

_CONDITION_NAME = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:state\.)?([A-Za-z_][A-Za-z0-9_]*)(\.[A-Za-z_][A-Za-z0-9_]*)*"
)


def _condition_segments(value: str) -> list[tuple[bool, str]]:
    """Split a yamlgraph condition into quoted and expression segments."""
    result: list[tuple[bool, str]] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is None:
            if character in {"'", '"'}:
                if index > start:
                    result.append((False, value[start:index]))
                quote = character
                start = index
            continue
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            result.append((True, value[start : index + 1]))
            quote = None
            start = index + 1
    if start < len(value):
        result.append((quote is not None, value[start:]))
    return result


def _rewrite_condition_references(
    value: str,
    mapping: dict[str, str],
    *,
    reject_unknown: bool = False,
) -> str:
    """Rewrite only parsed state paths outside quoted literal spans."""
    keywords = {"and", "or", "not", "true", "false", "null", "none"}
    unknown: set[str] = set()

    def rewrite(match: re.Match[str]) -> str:
        token = match.group(0)
        state_prefix = "state." if token.startswith("state.") else ""
        path = token.removeprefix("state.")
        root, separator, tail = path.partition(".")
        replacement = mapping.get(root)
        if replacement is None:
            if root.lower() not in keywords:
                unknown.add(root)
            return token
        return state_prefix + replacement + (separator + tail if separator else "")

    rewritten = "".join(
        segment if quoted else _CONDITION_NAME.sub(rewrite, segment)
        for quoted, segment in _condition_segments(value)
    )
    if reject_unknown and unknown:
        raise ValueError(
            f"fragment condition references unknown state: {sorted(unknown)}"
        )
    return rewritten


def _split_condition_keyword(value: str, keyword: str) -> list[str] | None:
    pieces: list[str] = []
    current: list[str] = []
    needle = f" {keyword} "
    segments = _condition_segments(value)
    for quoted, segment in segments:
        if quoted:
            current.append(segment)
            continue
        while needle in segment:
            before, segment = segment.split(needle, 1)
            current.append(before)
            pieces.append("".join(current))
            current = []
        current.append(segment)
    pieces.append("".join(current))
    return pieces if len(pieces) > 1 else None


def _condition_may_match_outcome(
    condition: str | None, result_key: str, outcome: str
) -> bool:
    """Conservative abstract evaluation for one protected result outcome."""
    if condition is None:
        return True
    or_parts = _split_condition_keyword(condition, "or")
    if or_parts is not None:
        return any(
            _condition_may_match_outcome(part, result_key, outcome) for part in or_parts
        )
    and_parts = _split_condition_keyword(condition, "and")
    if and_parts is not None:
        return all(
            _condition_may_match_outcome(part, result_key, outcome)
            for part in and_parts
        )
    comparison = re.fullmatch(
        rf"\s*(?:state\.)?{re.escape(result_key)}\.outcome\s*(==|!=)\s*"
        r"(['\"])(PASS|FAIL|ERROR)\2\s*",
        condition,
    )
    if comparison is None:
        return True
    operator, _quote, expected = comparison.groups()
    return (outcome == expected) if operator == "==" else (outcome != expected)
