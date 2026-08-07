"""Task 3: the lockstep recipe profile — pure YAML analysis, NO yamlgraph
import (this module must stay usable to vet a recipe before it is ever
compiled). Every rule here is enforced structurally against the dict
`yaml.safe_load` returns; it does not know or care whether the recipe would
actually compile under yamlgraph — that is `yamlgraph_api.cli_validate`'s
job (Task 1), run alongside this one wherever recipes are validated
end-to-end (Task 6's `validate_recipe`).

Rules enforced (errors unless noted; see plan Task 3 + SPIKE FINDINGS):

- Forbidden node types `llm, agent, router, copilot, race` (Global
  Constraints) anywhere in `nodes:`.
- No top-level `checkpointer:` block (decision 8 — only the engine controls
  persistence).
- Escalate-marker discriminator (decision 9): an `interrupt` node is a
  "work interrupt" unless its `message` is exactly `{step: escalate}` (plus
  optional `text`). Work interrupts alone are subject to validator-pairing
  and brief-field rules; marked nodes are exempt.
- Every work interrupt's outgoing edges must ALL target one single node,
  and that node must be a `python` node (its validator) — kills bypass
  edges and undiscovered validators.
- Every work interrupt's `message` brief must declare `step`/`task`/
  `exit_criterion` and at least one check.
- Every interrupt node (work OR escalate-marked) must declare
  `idempotent: false` (spike finding 4 — `prepare_fn`'s default
  `idempotent: true` reuses a stale payload across any interrupt sharing
  `state_key: brief`).
- Every retry loop must be capped: this module DFS-walks the `edges:`
  graph from `START` (conditional targets included); a conditional edge
  whose target is already on the current DFS stack is a back edge. Per
  spike finding 2, `loop_limits`/`loop_exits` are keyed on the REPEATING
  node — the back edge's SOURCE (the python validator), never the
  interrupt it loops back to — so every back-edge source must appear in
  both `loop_limits` and `loop_exits`.
- Per spike finding 3, `loop_exits` may never target an interrupt directly
  (yamlgraph skips that interrupt's `prepare_fn`, so it parks with a stale
  `brief` instead of the escalate marker): the `loop_exits` target must be
  a `passthrough` gate with exactly one outgoing edge, and that edge's
  target must be a marked escalate interrupt.
- `command_from` anywhere in a check config is forbidden (decision 7/review
  C2 — commands are pinned literally in the recipe, never taken from
  evidence).
- Placeholder substitution never reaches `checks`/`evidence_schema`
  (decision 4): any string therein matching `\\{[A-Za-z_]\\w*\\}` is an
  error (regex quantifiers like `\\d{3}` and JSON-schema `pattern` braces
  don't collide with this pattern — letters/underscore only).
- Every `path_from: key` check requires `evidence_schema.properties[key]
  .format == "project-path"` (decision 12).
- Any baseline check (`fresh`/`unchanged`/`changed_in`/`diff_only`) present
  while top-level `baseline_globs` is absent/empty is an error (review-5
  m4 — else the check errors forever at runtime, never a vacuous pass).
- A `tools:` entry whose `module` is not under `lockstep_mcp.` is a
  WARNING, not an error (local `tools.py` — last resort, human review).

Conditional-edge dialect (spike finding 1): edges are `{from, to,
condition}` triples. An edge dict carrying a `conditions:` list (the
`type: conditional` router shape) is a different, unsupported dialect —
flagged as an invalid edge shape rather than silently parsed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_NODE_TYPES = {"llm", "agent", "router", "copilot", "race"}
BASELINE_CHECK_TYPES = {"fresh", "unchanged", "changed_in", "diff_only"}
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_]\w*\}")


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
    (source, target) pairs, source = the repeating node (finding 2)."""
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
    seen_sources: set[str] = set()
    for src, tgt in _find_back_edges(edges_by_from):
        if src in seen_sources:
            continue
        seen_sources.add(src)

        if src not in loop_limits:
            errors.append(
                f"loop_limits: node '{src}' loops back to '{tgt}' without a loop_limits cap"
            )
        if src not in loop_exits:
            errors.append(
                f"loop_exits must target a passthrough gate for looping node '{src}' "
                "(no loop_exits entry)"
            )
            continue

        exit_target_name = loop_exits[src]
        exit_target = nodes.get(exit_target_name)
        if exit_target is None:
            errors.append(
                f"loop_exits must target an existing node ('{src}' -> "
                f"'{exit_target_name}' not found)"
            )
            continue
        if exit_target.get("type") == "interrupt":
            errors.append(
                "escalate must be gated through passthrough — yamlgraph skips "
                f"interrupt prepare on loop_exits (loop_exits['{src}'] points "
                f"directly at interrupt '{exit_target_name}')"
            )
            continue
        if exit_target.get("type") != "passthrough":
            errors.append(
                f"loop_exits must target a passthrough gate ('{src}' -> "
                f"'{exit_target_name}' is type {exit_target.get('type')!r})"
            )
            continue

        gate_edges = edges_by_from.get(exit_target_name, [])
        if len(gate_edges) != 1:
            errors.append(
                "loop_exits must target a passthrough gate with exactly one "
                f"outgoing edge (gate '{exit_target_name}' has {len(gate_edges)})"
            )
            continue

        final_target_name = gate_edges[0].get("to")
        final_target = nodes.get(final_target_name) if final_target_name else None
        if final_target is None or not _is_escalate_marker(final_target.get("message") or {}):
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
) -> None:
    message = node.get("message") or {}
    checks = message.get("checks") or []
    schema = message.get("evidence_schema")

    # finding 4: idempotent: false required on EVERY interrupt, work or
    # escalate-marked (a shared state_key: brief means the default
    # idempotent: true reuses whichever payload parked first).
    if node.get("idempotent") is not False:
        errors.append(f"interrupt '{name}' must declare idempotent: false")

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

    if any(isinstance(c, dict) and c.get("type") in BASELINE_CHECK_TYPES for c in checks):
        if not doc.get("baseline_globs"):
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
        for s in _walk_strings(schema):
            if PLACEHOLDER_RE.search(s):
                errors.append(
                    f"placeholder found in evidence_schema (interrupt '{name}'): {s!r}"
                )
                break


def check_recipe_full(path: str | Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    with open(path) as f:
        doc = yaml.safe_load(f) or {}

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

    for name, node in nodes.items():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        _check_interrupt_node(name, node, edges_by_from, nodes, doc, errors)

    _check_loops(
        edges_by_from,
        nodes,
        doc.get("loop_limits") or {},
        doc.get("loop_exits") or {},
        errors,
    )

    for tname, tcfg in (doc.get("tools") or {}).items():
        module = tcfg.get("module") if isinstance(tcfg, dict) else None
        if module and not module.startswith("lockstep_mcp."):
            warnings.append(
                f"local tools.py: tool '{tname}' references module '{module}' outside "
                "lockstep_mcp — human review recommended"
            )

    return errors, warnings


def check_recipe(path: str | Path) -> list[str]:
    errors, _warnings = check_recipe_full(path)
    return errors
