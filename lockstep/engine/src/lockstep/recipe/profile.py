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
from pathlib import Path
from typing import Any

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

    def __init__(
        self,
        recipe_bytes: bytes,
        *,
        context: str,
        compiler_version: str,
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
        object.__setattr__(self, "_recipe_bytes", recipe_bytes)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "compiler_version", compiler_version)
        object.__setattr__(
            self, "recipe_sha256", hashlib.sha256(recipe_bytes).hexdigest()
        )

    def matches(self, recipe_bytes: bytes) -> bool:
        return self._recipe_bytes == recipe_bytes


def _create_compiler_provenance(
    recipe_bytes: bytes,
    *,
    context: str,
    compiler_version: str = COMPILER_CONTRACT_VERSION,
) -> CompilerProvenance:
    """Issue an exact-byte compiler capability for trusted internal callers."""

    return CompilerProvenance(
        recipe_bytes,
        context=context,
        compiler_version=compiler_version,
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
        edges_by_from.setdefault(e.get("from"), []).append(e)
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
            to = e.get("to")
            if to is None or to == "END":
                continue
            if to in stack:
                back_edges.append((node, to))
            elif to not in visited:
                dfs(to)
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

        final_target_name = gate_edges[0].get("to")
        final_target = nodes.get(final_target_name) if final_target_name else None
        final_message = final_target.get("message") if isinstance(final_target, dict) else None
        protected = isinstance(final_message, dict) and "lockstep_effect" in final_message
        looks_like_escalate = (
            isinstance(final_message, dict)
            and final_message.get("step") == "escalate"
        )
        if looks_like_escalate and not (
            _is_escalate_marker(final_message or {}) or protected
        ):
            errors.append(
                f"escalate marker: loop_exits chain from '{src}' via "
                f"'{exit_target_name}' does not terminate on a {{step: escalate}} interrupt"
            )


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
        # yamlgraph types `message` as `str | dict`; a bare string is legal
        # there and meaningless here. Report it as the recipe error it is —
        # unguarded, it escapes `check_recipe` as an AttributeError and an
        # authoring mistake reads as an engine crash.
        errors.append(f"interrupt '{name}': message must be a mapping (a brief), not a string")
        return
    checks = message.get("checks") or []
    schema = message.get("evidence_schema")

    # idempotent: false required on EVERY interrupt, work or
    # escalate-marked (a shared state_key: brief means the default
    # idempotent: true reuses whichever payload parked first).
    if node.get("idempotent") is not False:
        errors.append(f"interrupt '{name}' must declare idempotent: false")

    if "lockstep_effect" in message:
        try:
            descriptor = parse_effect_descriptor(
                message["lockstep_effect"],
                known_state_keys=set(doc.get("state") or {}),
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"invalid lockstep_effect (interrupt '{name}'): {exc}")
            return
        if isinstance(descriptor, ScopeDescriptor) and not compiler_authorized:
            errors.append(
                f"scope descriptor (interrupt '{name}') requires compiler provenance"
            )
        # Native protected interrupts route directly on their typed result.  The
        # legacy python-validator pairing and evidence brief rules below belong
        # only to ordinary human work interrupts.
        return

    if not _is_escalate_marker(message):
        # validator pairing: ALL outgoing edges must target one node, and
        # that node must be the python validator.
        targets = sorted({e.get("to") for e in edges_by_from.get(name, [])})
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

    # These apply to whatever checks the node's brief carries regardless of
    # marker status (an escalate marker brief has none, so these are no-ops
    # there in practice).
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
    return check_recipe_bytes(Path(path).read_bytes(), provenance)


def check_recipe(path: str | Path) -> list[str]:
    errors, _warnings = check_recipe_full(path)
    return errors
