"""Fail-closed exception-ratchet verification over committed Git evidence."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from jsonschema import Draft202012Validator
from architecture_candidate_policy import _recompute_historical
from architecture_source_index import build_source_index


@dataclass(frozen=True, slots=True)
class ManifestVerdict:
    valid: bool
    errors: tuple[str, ...]
    accepted_exceptions: tuple[str, ...]


_TOP = frozenset(("schema_version", "ratchet_version", "reference_commit", "scan_root", "population", "analyzer_digest", "primitive_digest", "allowlist_digest", "lifecycle_digest", "schema_digest", "threshold_digest", "exceptions"))
_EXCEPTION = frozenset(("entity", "kind", "trigger_reasons", "responsibility", "invariant", "focused_gate", "baseline_metrics", "source_sha256", "semantic_dependency_sha256", "member_closure_sha256", "review_evidence", "next_review_gate", "expires_on"))
_EVIDENCE = frozenset(("project_relative_artifact_path", "git_tree_artifact_path", "review_commit", "artifact_blob_sha256", "reviewer_role", "verdict", "finding_counts", "reviewed_semantic_dependency_sha256", "review_evidence_sha256"))
_EXPIRY = frozenset(("source_changed", "semantic_dependency_changed", "member_closure_changed", "any_metric_increased", "any_component_increased", "composite_score_increased", "new_domain", "new_lifecycle_cluster", "focused_gate_missing_or_renamed", "review_evidence_unverifiable", "analyzer_or_rule_version_changed"))
_ANALYZERS = ("architecture_source_index.py", "architecture_legacy_metrics.py", "architecture_call_resolver.py", "architecture_domain_lifecycle.py", "architecture_candidate_policy.py", "architecture_manifest_verifier.py", "architecture_diagnostics.py")
_RULES = {"allowlist": "architecture_effect_free_allowlist.json", "primitives": "architecture_effect_primitives.json", "lifecycle": "architecture_lifecycle.json", "schema": "architecture_metrics.schema.json", "thresholds": "architecture_thresholds.json"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_KINDS = ("function", "one_hop", "class", "file")
_REVIEW_GATES = frozenset(("task-12-final-source-review",
                           "post-task-12-roadmap-reevaluation"))
_ARCH = "lockstep/engine/tests/architecture/"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _plain(record):
    result = {}
    for field in fields(record):
        value = getattr(record, field.name)
        result[field.name] = dict(value) if isinstance(value, Mapping) else list(value) if isinstance(value, tuple) else value
    return result


def _git(repo, *args, text=False):
    result = subprocess.run(("git", *args), cwd=repo, check=False, capture_output=True, text=text)
    if result.returncode:
        message = result.stderr if text else result.stderr.decode(errors="replace")
        raise ValueError(message.strip())
    return result.stdout


def _show(repo, commit, path):
    return _git(repo, "show", f"{commit}:{path}")


def _blob(repo, commit, path):
    rows = _git(repo, "ls-tree", commit, "--", path, text=True).strip().splitlines()
    if len(rows) != 1:
        raise ValueError("review artifact must resolve to exactly one regular blob")
    metadata, listed = rows[0].split("\t", 1)
    mode, kind, _object = metadata.split()
    if (mode, kind, listed) != ("100644", "blob", path):
        raise ValueError("review artifact must be exactly one regular blob")
    return _show(repo, commit, path)


def _ancestor(repo, older, newer):
    if (not isinstance(older, str) or not isinstance(newer, str)
            or _COMMIT.fullmatch(older) is None or _COMMIT.fullmatch(newer) is None):
        return False
    return subprocess.run(("git", "merge-base", "--is-ancestor", older, newer), cwd=repo, check=False, capture_output=True).returncode == 0


def _top_values(manifest, errors):
    checks = ((type(manifest["schema_version"]) is int and manifest["schema_version"] == 1, "schema_version must equal integer 1"), (manifest["ratchet_version"] == "v1", "ratchet_version must equal v1"), (manifest["scan_root"] == "src/lockstep", "scan_root must equal src/lockstep"), (isinstance(manifest["reference_commit"], str) and _COMMIT.fullmatch(manifest["reference_commit"]) is not None, "reference_commit must be a lowercase commit id"), (isinstance(manifest["exceptions"], list), "exceptions must be an ordered array"), (isinstance(manifest["population"], list), "population must be an ordered array"))
    for valid, message in checks:
        if not valid:
            errors.append(message)
    for name in ("analyzer_digest", "primitive_digest", "allowlist_digest", "lifecycle_digest", "schema_digest", "threshold_digest"):
        if not isinstance(manifest.get(name), str) or _SHA.fullmatch(manifest[name]) is None:
            errors.append(f"{name} must be a lowercase SHA-256 digest")


def _population_values(rows, errors):
    paths = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"path", "source_sha256"}
                or not isinstance(row.get("source_sha256"), str)
                or _SHA.fullmatch(row["source_sha256"]) is None):
            errors.append("population entry is malformed"); continue
        raw = row.get("path")
        path = PurePosixPath(raw) if isinstance(raw, str) else PurePosixPath("/")
        if not isinstance(raw, str) or path.as_posix() != raw or path.is_absolute() or ".." in path.parts or not raw.startswith("src/lockstep/") or not raw.endswith(".py"):
            errors.append("population path is not normalized")
            continue
        paths.append(raw)
    if paths != sorted(set(paths)):
        errors.append("population must be unique and path-sorted")


def _basic(manifest, errors):
    if not isinstance(manifest, dict) or set(manifest) != _TOP:
        errors.append("manifest keys must be exact")
        return False
    _top_values(manifest, errors)
    if errors:
        return False
    _population_values(manifest["population"], errors)
    return not errors


def _historical(repo, commit, manifest, analyzer_version):
    paths = tuple(row["path"] for row in manifest["population"])
    tree_paths = tuple(sorted(path.removeprefix("lockstep/engine/") for path in
        _git(repo, "ls-tree", "-r", "--name-only", commit, "--",
             "lockstep/engine/src/lockstep", text=True).splitlines()
        if path.endswith(".py")))
    if tree_paths != paths:
        raise ValueError("population does not match exact production Git tree")
    files = {path: _show(repo, commit, "lockstep/engine/" + path) for path in paths}
    if any(hashlib.sha256(files[row["path"]]).hexdigest() != row["source_sha256"] for row in manifest["population"]):
        raise ValueError("population source digest mismatch")
    index = build_source_index(Path(repo) / "lockstep/engine", paths, files)
    rules = {}
    for name, filename in _RULES.items():
        raw = _show(repo, commit, _ARCH + filename)
        value = json.loads(raw)
        if raw != _canonical(value):
            raise ValueError(filename + " is not exact canonical JSON")
        rules[name] = value
    digests = {name: _digest(value) for name, value in rules.items()}
    analyzers = [{"path": name, "sha256": hashlib.sha256(_show(repo, commit, _ARCH + name)).hexdigest()} for name in _ANALYZERS]
    if _digest(analyzers) != manifest["analyzer_digest"]:
        raise ValueError("analyzer digest mismatch")
    names = {"allowlist": "allowlist_digest", "primitives": "primitive_digest", "lifecycle": "lifecycle_digest", "schema": "schema_digest", "thresholds": "threshold_digest"}
    for name, manifest_name in names.items():
        if digests[name] != manifest[manifest_name]:
            raise ValueError(manifest_name.removesuffix("_digest") + " digest mismatch")
    semantics, computed = _recompute_historical(
        index, tuple(rules["allowlist"]["targets"]), rules["primitives"],
        rules["lifecycle"], thresholds=rules["thresholds"],
        allowlist_digest=digests["allowlist"],
        schema_digest=digests["schema"], threshold_digest=digests["thresholds"],
        analyzer_version=analyzer_version,
        rule_version=manifest["ratchet_version"])
    return index, semantics, computed, rules["schema"]


@lru_cache(maxsize=32)
def _historical_cached(repo, commit, manifest_bytes, analyzer_version):
    return _historical(Path(repo), commit, json.loads(manifest_bytes), analyzer_version)


def _at_commit(repo, commit, manifest, analyzer_version):
    frozen = {key: value for key, value in manifest.items() if key != "exceptions"}
    return _historical_cached(str(repo), commit, _canonical(frozen), analyzer_version)


def _entities(report):
    return {identity: (kind, metric) for kind, values in (("function", report.functions), ("one_hop", report.one_hops), ("class", report.classes), ("file", report.files)) for identity, metric in values.items()}


def _reasons(kind, metric):
    result = ["hard:" + item for item in metric.hard_triggers]
    mixing = metric.signals.get("domain_mixing") or metric.signals.get("lifecycle_mixing")
    mixing = mixing or (kind == "class" and metric.signals.get("cohesion_components")) or (kind == "file" and metric.signals.get("definition_dependency_components"))
    if metric.composite_score >= 3 and mixing:
        result.extend("signal:" + key for key, value in metric.signals.items() if value)
    return result


def _semantic(identity, kind, metric, semantics):
    if kind in {"function", "class"}:
        return semantics.entities[identity].semantic_dependency_sha256
    if kind == "one_hop":
        return semantics.build_one_hop(root=metric.root, members=metric.members).semantic_dependency_sha256
    return semantics.files[identity].semantic_dependency_sha256


def _member_pairs(identity, kind, metric, index, semantics):
    if kind == "file":
        path = identity.partition("::")[0]
        pairs = [(identity, semantics.files[identity].semantic_dependency_sha256)]
        pairs += [(item, semantics.entities[item].semantic_dependency_sha256) for item in index.entities if item.partition("::")[0] == path]
        pairs += [(item, row.import_semantic_sha256) for item, row in index.imports.items() if item.partition("::")[0] == path]
        return pairs
    identities = (identity,) if kind == "function" else metric.members if kind == "one_hop" else tuple(dict.fromkeys((identity, *(item for item, row in index.entities.items() if row.parent == identity), *metric.bases)))
    return [(item, semantics.entities[item].semantic_dependency_sha256) for item in identities]


def _member_digest(pairs):
    value = bytearray(b"lockstep.architecture-members/v1\0")
    for identity, digest in pairs:
        value.extend(identity.encode()); value.append(0); value.extend(digest.encode()); value.append(0)
    return hashlib.sha256(value).hexdigest()


def _review_path(evidence, errors):
    project, tree = evidence.get("project_relative_artifact_path"), evidence.get("git_tree_artifact_path")
    valid = isinstance(project, str) and 0 < len(project.encode()) <= 4096 and "\0" not in project and "\\" not in project
    if valid:
        path = PurePosixPath(project)
        valid = not path.is_absolute() and path.as_posix() == project and all(part not in {"", ".", ".."} for part in path.parts) and path.parts[:2] == (".superpowers", "reviews") and len(path.parts) > 2
    if not valid or tree != "lockstep/" + project:
        errors.append("review path must be normalized and path namespaces must match")
        return None
    return tree


@lru_cache(maxsize=256)
def _focused(repo, current_commit, node):
    result = subprocess.run((sys.executable, "-m", "pytest", "--collect-only", "-q", node), cwd=repo, check=False, capture_output=True, text=True)
    return result.returncode == 0 and node.rsplit("::", 1)[-1] in result.stdout


def _evidence_claims(repo, current_commit, evidence, semantic, errors):
    commit = evidence.get("review_commit")
    if not _ancestor(repo, commit, current_commit): errors.append("review commit ancestor validation failed")
    if evidence.get("reviewer_role") != "architecture": errors.append("reviewer role must be architecture")
    if evidence.get("verdict") != "PASS": errors.append("review verdict must be PASS")
    if evidence.get("finding_counts") != {"critical": 0, "important": 0, "minor": 0}: errors.append("review finding counts must be C0/I0/M0")
    if evidence.get("reviewed_semantic_dependency_sha256") != semantic: errors.append("reviewed semantic digest mismatch")
    without = {key: value for key, value in evidence.items() if key != "review_evidence_sha256"}
    if evidence.get("review_evidence_sha256") != _digest(without): errors.append("review evidence digest mismatch")


def _artifact_evidence(repo, exception, evidence, semantic, path, errors):
    commit = evidence.get("review_commit")
    if path is None or not isinstance(commit, str): return
    try: blob = _blob(repo, commit, path)
    except ValueError as error: errors.append(str(error)); return
    if hashlib.sha256(blob).hexdigest() != evidence.get("artifact_blob_sha256"): errors.append("review artifact blob digest mismatch")
    text = blob.decode("utf-8", errors="replace")
    entities = re.findall(r"(?m)^Entity: `([^`]+)`\s*$", text)
    digests = re.findall(
        r"(?m)^Semantic dependency SHA-256: `([0-9a-f]{64})`\s*$", text)
    if entities != [exception["entity"]]: errors.append("review artifact entity mismatch")
    if digests != [semantic]: errors.append("review artifact semantic digest mismatch")


def _evidence(repo, current_commit, exception, semantic, errors):
    evidence = exception.get("review_evidence")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE:
        errors.append("review evidence keys must be exact"); return
    path = _review_path(evidence, errors)
    _evidence_claims(repo, current_commit, evidence, semantic, errors)
    _artifact_evidence(repo, exception, evidence, semantic, path, errors)


def _shape(exception, errors):
    if not isinstance(exception, dict) or set(exception) != _EXCEPTION:
        errors.append("exception keys must be exact"); return False
    valid = True
    if not isinstance(exception.get("entity"), str) or not exception["entity"]:
        errors.append("exception entity must be a non-empty string"); valid = False
    if exception.get("kind") not in _KINDS:
        errors.append("exception kind is invalid"); valid = False
    reasons = exception.get("trigger_reasons")
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(item, str) or not item for item in reasons)):
        errors.append("trigger_reasons must be a non-empty string array"); valid = False
    gates = exception.get("focused_gate")
    if (not isinstance(gates, list) or not gates
            or any(not isinstance(item, str) or not item for item in gates)):
        errors.append("focused_gate must be a non-empty string array"); valid = False
    if not isinstance(exception.get("baseline_metrics"), dict):
        errors.append("baseline_metrics must be an object"); valid = False
    for name in ("source_sha256", "semantic_dependency_sha256",
                 "member_closure_sha256"):
        value = exception.get(name)
        if not isinstance(value, str) or _SHA.fullmatch(value) is None:
            errors.append(name + " must be a lowercase SHA-256 digest"); valid = False
    for name in ("responsibility", "invariant", "next_review_gate"):
        if not isinstance(exception.get(name), str) or not exception[name].strip():
            errors.append("exception " + name + " must be non-empty"); valid = False
    if exception.get("next_review_gate") not in _REVIEW_GATES:
        errors.append("next_review_gate is not an allowed review boundary"); valid = False
    expiry = exception.get("expires_on")
    if not isinstance(expiry, dict) or set(expiry) != _EXPIRY:
        errors.append("expires_on keys must be exact"); valid = False
    elif any(value is not True for value in expiry.values()):
        errors.append("expires_on values must all be true"); valid = False
    return valid


def _metric_claims(exception, kind, metric, schema, errors):
    baseline = exception.get("baseline_metrics")
    try: Draft202012Validator(schema["$defs"][kind]).validate(baseline)
    except Exception: errors.append("baseline does not match exact per-kind schema")
    if baseline != _plain(metric): errors.append("baseline metrics are stale or worsened")
    if exception.get("trigger_reasons") != _reasons(kind, metric): errors.append("trigger reasons do not match recomputed candidate")


def _digest_claims(exception, identity, kind, metric, index, semantics, errors):
    source_identity = metric.root if kind == "one_hop" else identity
    path = source_identity.partition("::")[0]
    source = index.file_sha256[path] if kind == "file" else index.entities[source_identity].span.sha256
    semantic = _semantic(identity, kind, metric, semantics)
    closure = _member_digest(_member_pairs(identity, kind, metric, index, semantics))
    if exception.get("source_sha256") != source: errors.append("source digest changed")
    if exception.get("semantic_dependency_sha256") != semantic: errors.append("semantic dependency digest changed")
    if exception.get("member_closure_sha256") != closure: errors.append("member closure digest changed")


def _validate_exception(repo, current_commit, exception, current, reviewed,
                        current_index, current_semantics, reviewed_semantics,
                        schema, errors):
    if not _shape(exception, errors): return
    identity, kind = exception["entity"], exception["kind"]
    if identity not in current: errors.append("stale exception for disappeared entity"); return
    current_kind, current_metric = current[identity]
    if current_kind != kind: errors.append("exception kind does not match entity")
    if not current_metric.candidate: errors.append("stale exception for noncandidate entity")
    if identity not in reviewed: errors.append("historical recomputation is missing exception entity"); return
    historical_kind, metric = reviewed[identity]
    if historical_kind != kind: errors.append("reviewed exception kind mismatch")
    _metric_claims(exception, kind, metric, schema, errors)
    _digest_claims(exception, identity, kind, current_metric,
                   current_index, current_semantics, errors)
    reviewed_semantic = _semantic(
        identity, kind, metric, reviewed_semantics)
    gates = exception.get("focused_gate")
    if not isinstance(gates, list) or not gates or any(not isinstance(node, str) or not _focused(repo, current_commit, node) for node in gates): errors.append("focused gate missing or failed collection")
    _evidence(repo, current_commit, exception, reviewed_semantic, errors)


def _current_context(repo, current_commit, report, manifest, errors):
    if not _ancestor(repo, manifest["reference_commit"], current_commit): errors.append("reference commit must be an ancestor")
    if report.unresolved_callsites: errors.append("unresolved callsites remain")
    for name, value in (("allowlist_digest", report.allowlist_digest), ("primitive_digest", report.primitive_digest), ("lifecycle_digest", report.lifecycle_digest), ("schema_digest", report.schema_digest), ("threshold_digest", report.threshold_digest)):
        if manifest[name] != value: errors.append(name.removesuffix("_digest") + " digest mismatch")
    try:
        _reference = _at_commit(
            repo, manifest["reference_commit"], manifest, report.analyzer_version)
        current_index, current_semantics, computed, _current_schema = _at_commit(
            repo, current_commit, manifest, report.analyzer_version)
    except Exception as error:
        errors.append("historical recomputation failed: " + str(error)); return None
    if computed != report:
        errors.append("historical recomputation of current commit does not match supplied report")
    return current_index, current_semantics, computed


def _review_rows(repo, current_commit, report, manifest, context, errors):
    current_index, current_semantics, computed = context
    current, supplied = _entities(computed), _entities(report)
    identities = [item.get("entity") for item in manifest["exceptions"] if isinstance(item, dict)]
    if len(identities) != len(set(identities)): errors.append("duplicate exception entity")
    accepted = []
    for exception in manifest["exceptions"]:
        before = len(errors)
        claimed = supplied.get(exception["entity"])
        if claimed is not None and not claimed[1].candidate:
            errors.append("stale exception for supplied noncandidate entity")
        review_commit = exception["review_evidence"].get("review_commit")
        if not _ancestor(repo, review_commit, current_commit):
            errors.append("review commit ancestor validation failed")
            continue
        try:
            reviewed_index, reviewed_semantics, reviewed_report, schema = _at_commit(
                repo, review_commit, manifest, report.analyzer_version)
        except Exception as error:
            errors.append("review historical recomputation failed: " + str(error))
            continue
        _validate_exception(
            repo, current_commit, exception, current, _entities(reviewed_report),
            current_index, current_semantics, reviewed_semantics, schema, errors)
        if len(errors) == before: accepted.append(exception["entity"])
    return current, accepted


def verify_manifest(report, manifest, *, repo_root, current_commit):
    errors = []
    if not _basic(manifest, errors): return ManifestVerdict(False, tuple(errors), ())
    if any(not isinstance(item, dict) or set(item) != _EXCEPTION
           for item in manifest["exceptions"]):
        errors.append("exception keys must be exact")
        return ManifestVerdict(False, tuple(errors), ())
    if any(not isinstance(item.get("review_evidence"), dict)
           or set(item["review_evidence"]) != _EVIDENCE
           for item in manifest["exceptions"]):
        errors.append("review evidence keys must be exact")
        return ManifestVerdict(False, tuple(errors), ())
    if any(not _shape(item, errors) for item in manifest["exceptions"]):
        return ManifestVerdict(False, tuple(errors), ())
    repo = Path(repo_root)
    context = _current_context(repo, current_commit, report, manifest, errors)
    if context is None:
        return ManifestVerdict(False, tuple(errors), ())
    current, accepted = _review_rows(
        repo, current_commit, report, manifest, context, errors)
    missing = sorted(identity for identity, (_kind, metric) in current.items() if metric.candidate and identity not in accepted)
    if missing: errors.append("candidate remains without valid exception: " + ", ".join(missing))
    return ManifestVerdict(not errors, tuple(errors), tuple(accepted))
