"""the lockstep recipe profile — pure YAML analysis, NO yamlgraph
import (this module must stay usable to vet a recipe before it is ever
compiled). Every rule here is enforced structurally against the dict
`yaml.safe_load` returns; it does not know or care whether the recipe would
actually compile under yamlgraph — that is `yamlgraph_api.cli_validate`'s
job, run alongside this one wherever recipes are validated
end-to-end.

Rules enforced (errors unless noted):

- Forbidden node types `llm, agent, router, copilot, race` (Global
  Constraints) anywhere in `nodes:`.
- No top-level `checkpointer:` block — only the engine controls
  persistence.
- Escalate-marker discriminator: an `interrupt` node is a
  "work interrupt" unless its `message` is exactly `{step: escalate}` (plus
  optional `text`). Work interrupts alone are subject to validator-pairing
  and brief-field rules; marked nodes are exempt.
- Every work interrupt's outgoing edges must ALL target one single node,
  and that node must be a `python` node (its validator) — kills bypass
  edges and undiscovered validators.
- Every work interrupt's `message` brief must declare `step`/`task`/
  `exit_criterion` and at least one check; work-interrupt `step` names are
  unique across the recipe (spawn prediction and `done()` key on them).
- Every interrupt node (work OR escalate-marked) must declare
  `idempotent: false` — `prepare_fn`'s default `idempotent: true` reuses
  a stale payload across any interrupt sharing `state_key: brief`.
- Every retry loop must be capped: this module DFS-walks the `edges:`
  graph from `START` (conditional targets included); a conditional edge
  whose target is already on the current DFS stack is a back edge.
  `loop_limits`/`loop_exits` are keyed on the REPEATING node — the back edge's SOURCE (the python validator), never the
  interrupt it loops back to — so every back-edge source must appear in
  both `loop_limits` and `loop_exits`.
- `loop_exits` may never target an interrupt directly
  (yamlgraph skips that interrupt's `prepare_fn`, so it parks with a stale
  `brief` instead of the escalate marker): the `loop_exits` target must be
  a `passthrough` gate with exactly one outgoing edge, and that edge's
  target must be a marked escalate interrupt.
- `command_from` anywhere in a check config is forbidden — commands are
  pinned literally in the recipe, never taken from evidence.
- Placeholder substitution never reaches `checks`/`evidence_schema`: any
  string therein matching `\\{[A-Za-z_]\\w*\\}` is an
  error (regex quantifiers like `\\d{3}` and JSON-schema `pattern` braces
  don't collide with this pattern — letters/underscore only).
- Every `path_from: key` check requires `evidence_schema.properties[key]
  .format == "project-path"`.
- Any baseline check (`fresh`/`unchanged`/`changed_in`/`diff_only`) present
  while top-level `baseline_globs` is absent/empty is an error — else the
  check errors forever at runtime, never a vacuous pass.
- A `tools:` entry whose `module` is not under `lockstep.` is a
  WARNING, not an error (local `tools.py` — last resort, human review).

Conditional-edge dialect: edges are `{from, to,
condition}` triples. An edge dict carrying a `conditions:` list (the
`type: conditional` router shape) is a different, unsupported dialect —
flagged as an invalid edge shape rather than silently parsed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import ScopeDescriptor

FORBIDDEN_NODE_TYPES = {"llm", "agent", "router", "copilot", "race"}
BASELINE_CHECK_TYPES = {"fresh", "unchanged", "changed_in", "diff_only"}
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_]\w*\}")
COMPILER_CONTRACT_VERSION = "1"
_PROVENANCE_CONTEXTS = frozenset({"compiler-output", "canonical-match"})
_PROVENANCE_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class CompilerProvenance:
    """In-memory capability binding compiler authority to exact recipe bytes.

    Project YAML cannot construct this capability.  The compiler and canonical
    freshness verifier use the private factory below after producing or proving
    the complete byte sequence respectively.
    """

    _recipe_bytes: bytes = field(repr=False)
    context: str
    compiler_version: str
    recipe_sha256: str
    files: tuple["ProvenanceFile", ...]
    root_relative_path: str
    bundle_sha256: str
    source_bundle_sha256: str

    def __init__(
        self,
        recipe_bytes: bytes,
        *,
        context: str,
        compiler_version: str,
        root_relative_path: str = "root.recipe.yaml",
        generated_files: Mapping[str, bytes] | None = None,
        execution_recipe_bytes: bytes | None = None,
        execution_generated_files: Mapping[str, bytes] | None = None,
        source_bundle_sha256: str | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _PROVENANCE_FACTORY_TOKEN:
            raise TypeError("CompilerProvenance is issued only by the compiler verifier")
        if context not in _PROVENANCE_CONTEXTS:
            raise ValueError("unsupported compiler provenance context")
        if compiler_version != COMPILER_CONTRACT_VERSION:
            raise ValueError("unsupported compiler provenance version")
        if not isinstance(recipe_bytes, bytes):
            raise TypeError("compiler provenance recipe bytes must be bytes")
        execution_root = execution_recipe_bytes or recipe_bytes
        if not isinstance(execution_root, bytes):
            raise TypeError("canonical execution recipe bytes must be bytes")
        source_generated = dict(generated_files or {})
        execution_generated = dict(execution_generated_files or source_generated)
        if set(source_generated) != set(execution_generated):
            raise ValueError("source and execution provenance file sets differ")
        object.__setattr__(self, "_recipe_bytes", execution_root)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "compiler_version", compiler_version)
        object.__setattr__(
            self, "recipe_sha256", hashlib.sha256(execution_root).hexdigest()
        )
        members = [ProvenanceFile.build(root_relative_path, execution_root, "root")]
        members.extend(
            ProvenanceFile.build(path, content, "specialized-child")
            for path, content in sorted(execution_generated.items())
        )
        paths = tuple(item.relative_path for item in members)
        if len(paths) != len(set(paths)):
            raise ValueError("compiler provenance contains duplicate file paths")
        if members[0].relative_path != root_relative_path or members[0].role != "root":
            raise ValueError("compiler provenance root association is invalid")
        manifest = hashlib.sha256(b"lockstep.compiler-provenance/v1\0")
        for item in members:
            manifest.update(item.relative_path.encode("utf-8"))
            manifest.update(b"\0")
            manifest.update(item.sha256.encode("ascii"))
            manifest.update(b"\0")
        bundle_sha256 = manifest.hexdigest()
        object.__setattr__(self, "files", tuple(members))
        object.__setattr__(self, "root_relative_path", root_relative_path)
        object.__setattr__(self, "bundle_sha256", bundle_sha256)
        object.__setattr__(
            self, "source_bundle_sha256", source_bundle_sha256 or bundle_sha256
        )

    def matches(self, recipe_bytes: bytes) -> bool:
        return self._recipe_bytes == recipe_bytes

    def matches_member(self, relative_path: str, recipe_bytes: bytes) -> bool:
        return any(
            item.relative_path == relative_path
            and item.canonical_execution_bytes == recipe_bytes
            for item in self.files
        )


@dataclass(frozen=True, slots=True)
class ProvenanceFile:
    relative_path: str
    canonical_execution_bytes: bytes = field(repr=False)
    sha256: str
    role: str

    @classmethod
    def build(
        cls, relative_path: str, content: bytes, role: str
    ) -> "ProvenanceFile":
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("provenance relative path must be non-empty")
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or relative_path != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("provenance path must be canonical and contained")
        if not isinstance(content, bytes):
            raise TypeError("provenance content must be bytes")
        if role not in {"root", "specialized-child"}:
            raise ValueError("unsupported provenance file role")
        return cls(
            relative_path, content, hashlib.sha256(content).hexdigest(), role
        )


def _create_compiler_provenance(
    recipe_bytes: bytes,
    *,
    context: str,
    compiler_version: str = COMPILER_CONTRACT_VERSION,
    root_relative_path: str = "root.recipe.yaml",
    generated_files: Mapping[str, bytes] | None = None,
    execution_recipe_bytes: bytes | None = None,
    execution_generated_files: Mapping[str, bytes] | None = None,
    source_bundle_sha256: str | None = None,
) -> CompilerProvenance:
    """Issue an exact-byte compiler capability for trusted internal callers."""

    return CompilerProvenance(
        recipe_bytes,
        context=context,
        compiler_version=compiler_version,
        root_relative_path=root_relative_path,
        generated_files=generated_files,
        execution_recipe_bytes=execution_recipe_bytes,
        execution_generated_files=execution_generated_files,
        source_bundle_sha256=source_bundle_sha256,
        _token=_PROVENANCE_FACTORY_TOKEN,
    )


def _check_provenance(
    recipe_bytes: bytes,
    provenance: CompilerProvenance | None,
    errors: list[str],
) -> bool:
    if provenance is None:
        return False
    if not isinstance(provenance, CompilerProvenance):
        errors.append("compiler provenance capability is invalid")
        return False
    if provenance.context not in _PROVENANCE_CONTEXTS:
        errors.append("compiler provenance context is invalid")
        return False
    if provenance.compiler_version != COMPILER_CONTRACT_VERSION:
        errors.append("compiler provenance version does not match this profile")
        return False
    if not provenance.matches(recipe_bytes):
        errors.append("compiler provenance does not match the exact recipe bytes")
        return False
    return True

def _is_escalate_marker(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("step") != "escalate":
        return False
    return set(message.keys()) <= {"step", "text"}


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _build_edges_by_from(raw_edges: list, errors: list[str]) -> dict[str, list[dict]]:
    edges_by_from: dict[str, list[dict]] = {}
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        if "conditions" in e:
            errors.append(
                "invalid edge shape: 'conditions' list form is not supported "
                f"(edge from {e.get('from')!r}) — use {{from, to, condition}} triples"
            )
            continue
        raw_sources = e.get("from")
        sources = raw_sources if isinstance(raw_sources, list) else [raw_sources]
        for source in sources:
            if isinstance(source, str):
                edges_by_from.setdefault(source, []).append(e)
            else:
                errors.append("invalid edge source: expected a node name")
    return edges_by_from


def _find_back_edges(edges_by_from: dict[str, list[dict]]) -> list[tuple[str, str]]:
    """DFS from START (conditional targets included). A conditional edge
    whose target is already on the current DFS stack is a back edge —
    (source, target) pairs, source = the repeating node."""
    back_edges: list[tuple[str, str]] = []
    visited: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> None:
        visited.add(node)
        stack.append(node)
        for e in edges_by_from.get(node, []):
            raw_targets = e.get("to")
            targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
            for target in targets:
                if target is None or target == "END":
                    continue
                if target in stack:
                    back_edges.append((node, target))
                elif target not in visited:
                    dfs(target)
        stack.pop()

    if edges_by_from.get("START"):
        dfs("START")
    return back_edges


def _check_loops(
    edges_by_from: dict[str, list[dict]],
    nodes: dict[str, dict],
    loop_limits: dict,
    loop_exits: dict,
    errors: list[str],
) -> None:
    for source, target_name in loop_exits.items():
        target = nodes.get(target_name)
        if isinstance(target, dict) and target.get("type") == "interrupt":
            errors.append(
                "loop_exits must be gated through passthrough and may not target "
                "an interrupt directly — yamlgraph "
                f"skips interrupt prepare (loop_exits['{source}'] -> '{target_name}')"
            )
    seen_sources: set[str] = set()
    for src, tgt in _find_back_edges(edges_by_from):
        # Legacy recipes cap the repeating back-edge source (validator).
        # Native lowering caps the attempt gate before the protected effect,
        # which is the back-edge target. Both are real yamlgraph node limits.
        capped = tgt if tgt in loop_limits else src
        if capped in seen_sources:
            continue
        seen_sources.add(capped)

        cap = loop_limits.get(capped)
        if capped not in loop_limits:
            errors.append(
                f"loop_limits: node '{src}' loops back to '{tgt}' without a loop_limits cap"
            )
        elif not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            # Presence is not a cap: `null`, `0`, `-1` and `"lots"` all read
            # as "declared" while capping nothing.
            errors.append(
                f"loop_limits: node '{src}' cap must be a positive integer, got {cap!r}"
            )
        if capped not in loop_exits:
            errors.append(
                f"loop_exits must target a passthrough gate for looping node '{src}' "
                "(no loop_exits entry)"
            )
            continue

        exit_target_name = loop_exits[capped]
        exit_target = nodes.get(exit_target_name)
        if exit_target is None:
            errors.append(
                f"loop_exits must target an existing node ('{src}' -> "
                f"'{exit_target_name}' not found)"
            )
            continue
        if exit_target.get("type") == "interrupt":
            continue
        gate_edges = edges_by_from.get(exit_target_name, [])
        if len(gate_edges) != 1:
            continue

        raw_final_targets = gate_edges[0].get("to")
        final_targets = (
            raw_final_targets
            if isinstance(raw_final_targets, list)
            else [raw_final_targets]
        )
        for final_target_name in final_targets:
            final_target = (
                nodes.get(final_target_name)
                if isinstance(final_target_name, str) and final_target_name
                else None
            )
            final_message = (
                final_target.get("message")
                if isinstance(final_target, dict)
                else None
            )
            protected = (
                isinstance(final_message, dict)
                and "lockstep_effect" in final_message
            )
            looks_like_escalate = (
                isinstance(final_message, dict)
                and final_message.get("step") == "escalate"
            )
            if looks_like_escalate and not (
                _is_escalate_marker(final_message or {}) or protected
            ):
                errors.append(
                    f"escalate marker: loop_exits chain from '{src}' via "
                    f"'{exit_target_name}' does not terminate on a "
                    "{step: escalate} interrupt"
                )


def _check_protected_interrupt(
    name: str,
    node: dict,
    message: dict,
    doc: dict,
    errors: list[str],
    *,
    compiler_authorized: bool,
) -> bool:
    if "lockstep_effect" not in message:
        return False
    try:
        descriptor = parse_effect_descriptor(
            message["lockstep_effect"],
            known_state_keys=set(doc.get("state") or {}),
        )
    except (TypeError, ValueError) as exc:
        errors.append(f"invalid lockstep_effect (interrupt '{name}'): {exc}")
        return True
    if isinstance(descriptor, ScopeDescriptor) and not compiler_authorized:
        errors.append(
            f"scope descriptor (interrupt '{name}') requires compiler provenance"
        )
    if (
        isinstance(descriptor, ScopeDescriptor)
        and node.get("resume_key") != descriptor.result_state_key
    ):
        errors.append(
            f"scope descriptor (interrupt '{name}') result_state_key must equal resume_key"
        )
    return True


def _check_work_interrupt_route(
    name: str,
    message: dict,
    checks: list,
    edges_by_from: dict[str, list[dict]],
    nodes: dict[str, dict],
    errors: list[str],
) -> None:
    if _is_escalate_marker(message):
        return
    targets = sorted(
        {
            target
            for edge in edges_by_from.get(name, [])
            for target in (
                edge.get("to")
                if isinstance(edge.get("to"), list)
                else [edge.get("to")]
            )
        }
    )
    if not targets:
        errors.append(
            f"no validator: work interrupt '{name}' has no outgoing edge to a validator node"
        )
    elif len(targets) > 1:
        errors.append(
            f"bypass: work interrupt '{name}' has edges to multiple targets "
            f"{targets} — only the validator edge is allowed"
        )
    else:
        target_node = nodes.get(targets[0])
        if target_node is None or target_node.get("type") != "python":
            errors.append(
                f"no validator: work interrupt '{name}' does not lead directly "
                f"to a python validator node (target {targets[0]!r})"
            )
    for field in ("step", "task", "exit_criterion"):
        if not message.get(field):
            errors.append(f"work interrupt '{name}' brief missing required field '{field}'")
    if not checks:
        errors.append(f"work interrupt '{name}' brief must declare at least one check")


def _check_interrupt_checks(
    name: str,
    checks: list,
    schema: object,
    doc: dict,
    errors: list[str],
) -> None:
    props = {}
    if isinstance(schema, dict):
        props = schema.get("properties") or {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        if "command_from" in check:
            errors.append(
                f"command_from is forbidden (interrupt '{name}'): commands must be "
                "pinned literally in the recipe"
            )
        key = check.get("path_from")
        if key:
            prop = props.get(key) or {}
            if prop.get("format") != "project-path":
                errors.append(
                    f"path_from key '{key}' missing project-path annotation "
                    f"('format: project-path' in evidence_schema, interrupt '{name}')"
                )

    if (
        any(isinstance(c, dict) and c.get("type") in BASELINE_CHECK_TYPES for c in checks)
        and not doc.get("baseline_globs")
    ):
        errors.append(
            f"baseline_globs must be declared when baseline checks are used (interrupt '{name}')"
            )

    for s in _walk_strings(checks):
        if PLACEHOLDER_RE.search(s):
            errors.append(
                f"placeholder found in checks (interrupt '{name}'): {s!r} — vars never "
                "reach checks, they must be verbatim"
            )
            break


def _check_interrupt_schema(
    name: str, schema: object, errors: list[str]
) -> None:
    if schema is not None:
        # A malformed schema raises SchemaError from every later
        # `iter_errors`, i.e. from inside `scenario_done` — after the recipe
        # already validated ok. Refuse it here, where a recipe error belongs.
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            errors.append(
                f"invalid evidence_schema (interrupt '{name}'): {exc.message}"
            )
        for s in _walk_strings(schema):
            if PLACEHOLDER_RE.search(s):
                errors.append(
                    f"placeholder found in evidence_schema (interrupt '{name}'): {s!r}"
                )
                break


def _check_interrupt_node(
    name: str,
    node: dict,
    edges_by_from: dict[str, list[dict]],
    nodes: dict[str, dict],
    doc: dict,
    errors: list[str],
    *,
    compiler_authorized: bool,
) -> None:
    message = node.get("message") or {}
    if not isinstance(message, dict):
        errors.append(
            f"interrupt '{name}': message must be a mapping (a brief), not a string"
        )
        return
    checks = message.get("checks") or []
    schema = message.get("evidence_schema")
    if node.get("idempotent") is not False:
        errors.append(f"interrupt '{name}' must declare idempotent: false")
    if _check_protected_interrupt(
        name,
        node,
        message,
        doc,
        errors,
        compiler_authorized=compiler_authorized,
    ):
        return
    _check_work_interrupt_route(name, message, checks, edges_by_from, nodes, errors)
    _check_interrupt_checks(name, checks, schema, doc, errors)
    _check_interrupt_schema(name, schema, errors)


def check_recipe_bytes(
    recipe_bytes: bytes,
    provenance: CompilerProvenance | None = None,
) -> tuple[list[str], list[str]]:
    if not isinstance(recipe_bytes, bytes):
        raise TypeError("recipe profile input must be bytes")
    errors: list[str] = []
    warnings: list[str] = []

    doc = yaml.safe_load(recipe_bytes) or {}
    compiler_authorized = _check_provenance(recipe_bytes, provenance, errors)

    if "x-lockstep-generated" in doc and not compiler_authorized:
        errors.append("x-lockstep-generated marker requires compiler provenance")

    nodes: dict[str, dict] = doc.get("nodes") or {}
    raw_edges: list = doc.get("edges") or []

    if "checkpointer" in doc:
        errors.append(
            "checkpointer: recipe must not declare a checkpointer block — the engine owns persistence"
        )

    for name, node in nodes.items():
        ntype = node.get("type") if isinstance(node, dict) else None
        if ntype in FORBIDDEN_NODE_TYPES:
            errors.append(f"forbidden node type: '{ntype}' (node '{name}')")

    edges_by_from = _build_edges_by_from(raw_edges, errors)

    # work-interrupt `step` names must be UNIQUE across the recipe —
    # Native resume identifies an exact interrupt coordinate, while the public
    # scenario_done compatibility surface still names its worker step. Keep
    # worker step names unique; graph-owned escalate markers are exempt.
    seen_steps: dict[str, str] = {}
    for name, node in nodes.items():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        msg = node.get("message") or {}
        if not isinstance(msg, dict):
            continue  # reported by the interrupt rules
        if "lockstep_effect" in msg:
            continue
        if _is_escalate_marker(msg):
            continue
        step = msg.get("step")
        if not isinstance(step, str) or not step:
            continue  # the missing-field rule reports this one
        if step in seen_steps:
            errors.append(
                f"duplicate step name '{step}' (interrupts '{seen_steps[step]}' and "
                f"'{name}') — spawn prediction and scenario_done are keyed on it"
            )
        else:
            seen_steps[step] = name

    for name, node in nodes.items():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        _check_interrupt_node(
            name,
            node,
            edges_by_from,
            nodes,
            doc,
            errors,
            compiler_authorized=compiler_authorized,
        )

    _check_loops(
        edges_by_from,
        nodes,
        doc.get("loop_limits") or {},
        doc.get("loop_exits") or {},
        errors,
    )

    for tname, tcfg in (doc.get("tools") or {}).items():
        module = tcfg.get("module") if isinstance(tcfg, dict) else None
        if module and not module.startswith("lockstep."):
            warnings.append(
                f"local tools.py: tool '{tname}' references module '{module}' outside "
                "lockstep — human review recommended"
            )

    return errors, warnings


def check_recipe_full(
    path: str | Path,
    provenance: CompilerProvenance | None = None,
) -> tuple[list[str], list[str]]:
    root = Path(path).resolve()
    errors, warnings = check_recipe_bytes(root.read_bytes(), provenance)
    visited = {root}

    def inspect_children(current: Path) -> None:
        try:
            document = yaml.safe_load(current.read_bytes())
        except (OSError, yaml.YAMLError):
            return
        nodes = document.get("nodes", {}) if isinstance(document, dict) else {}
        for node in nodes.values() if isinstance(nodes, dict) else ():
            if not isinstance(node, dict) or node.get("type") != "subgraph":
                continue
            graph = node.get("graph")
            if not isinstance(graph, str):
                continue
            child = current.parent / graph
            try:
                resolved = child.resolve(strict=True)
                resolved.relative_to(root.parent)
            except (OSError, ValueError):
                errors.append(f"child {graph!r} is not a contained readable recipe")
                continue
            if resolved in visited:
                continue
            visited.add(resolved)
            child_bytes = resolved.read_bytes()
            relative = resolved.relative_to(root.parent).as_posix()
            child_provenance = None
            if provenance is not None and provenance.matches_member(relative, child_bytes):
                child_provenance = _create_compiler_provenance(
                    child_bytes,
                    context=provenance.context,
                    root_relative_path=relative,
                    source_bundle_sha256=provenance.source_bundle_sha256,
                )
            child_errors, child_warnings = check_recipe_bytes(
                child_bytes, child_provenance
            )
            errors.extend(f"child {graph}: {item}" for item in child_errors)
            warnings.extend(f"child {graph}: {item}" for item in child_warnings)
            inspect_children(resolved)

    inspect_children(root)
    if provenance is not None:
        observed = {
            provenance.root_relative_path,
            *(path.relative_to(root.parent).as_posix() for path in visited if path != root),
        }
        proven = {item.relative_path for item in provenance.files}
        if observed != proven:
            errors.append(
                "compiler provenance file set does not match the complete reachable recipe DAG"
            )
    return errors, warnings


def check_recipe(path: str | Path) -> list[str]:
    errors, _warnings = check_recipe_full(path)
    return errors
