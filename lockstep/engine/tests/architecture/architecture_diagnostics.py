"""Pure canonical rendering for already computed architecture evidence."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import json


def _plain(value):
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _candidates(report):
    rows = []
    kinds = (("function", report.functions), ("one_hop", report.one_hops),
             ("class", report.classes), ("file", report.files))
    kind_order = {kind: position for position, (kind, _values) in enumerate(kinds)}
    for kind, values in kinds:
        for ast_order, (identity, metrics) in enumerate(values.items()):
            if metrics.candidate:
                rank = getattr(values, "ast_order", {}).get(identity, ast_order)
                rows.append((identity.partition("::")[0], rank,
                             kind_order[kind], identity,
                             {"identity": identity, "kind": kind,
                              "metrics": _plain(metrics)}))
    return [row[-1] for row in sorted(rows, key=lambda row: row[:-1])]


def render_report(report, verdict):
    value = {
        "candidates": _candidates(report),
        "digests": {
            "allowlist": report.allowlist_digest,
            "analyzer": report.analyzer_version,
            "lifecycle": report.lifecycle_digest,
            "primitive": report.primitive_digest,
            "rule_version": report.rule_version,
            "schema": report.schema_digest,
            "threshold": report.threshold_digest,
        },
        "manifest": {
            "accepted_exceptions": list(verdict.accepted_exceptions),
            "errors": list(verdict.errors),
            "valid": verdict.valid,
        },
        "unresolved_callsites": list(report.unresolved_callsites),
    }
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")) + "\n"
