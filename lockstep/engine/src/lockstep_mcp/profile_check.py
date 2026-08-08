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
- A `tools:` entry whose `module` is not under `lockstep_mcp.` is a
  WARNING, not an error (local `tools.py` — last resort, human review).

The subcall triple (spawn -> `_subcall` marker -> poll) is a
THIRD interrupt class alongside work and escalate interrupts. A
`{step: _subcall}` marker is exempt from the work-interrupt rules
(validator pairing, task/exit_criterion/checks) but not from
`idempotent: false` or the checks:/placeholder scans. Its own rules
(`_check_subcall_rules`): required `node` (unique, `^[a-z][a-z0-9_-]*$`)
and verbatim non-empty `prompt`; optional `runner` (`^[a-z][a-z0-9-]*$`),
positive-int `timeout_minutes`, fractal `scenario` (child recipe must
exist as `<child_recipes_dir or recipe-dir>/<name>.yaml`) and `artifacts`
(dot-free names, relative paths covered by the child's `baseline_globs`).
Triple shape: spawn -> marker is the spawn's single unconditional edge;
marker -> poll is the marker's single unconditional edge; every edge into
a spawn comes from a directly-paired validator conditioned exactly
`verdict_status == '(pass|fail)'` (what the engine's spawn prediction
keys on) —
never from START: a start-time spawn would fire on an empty evidence
channel and bypass the engine's done()-time policy prediction
(runner/budget/depth), so the profile forbids the shape outright. Every
poll out-edge is conditioned exactly
`_subcall_status == '(running|done|error)'` with the 'running' back edge
to the marker required. Poll back edges are exempt from
`loop_limits`/`loop_exits` (termination is the runner timeout). Subcall
recipes must declare `_subcall_status`/`_subcall_envelope` in `state:`;
every `hash_from` must match
`_subcall_envelope.artifact_hashes.<declared artifact>`.

Conditional-edge dialect: edges are `{from, to,
condition}` triples. An edge dict carrying a `conditions:` list (the
`type: conditional` router shape) is a different, unsupported dialect —
flagged as an invalid edge shape rather than silently parsed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from lockstep_mcp.validators import _path_covered

FORBIDDEN_NODE_TYPES = {"llm", "agent", "router", "copilot", "race"}
BASELINE_CHECK_TYPES = {"fresh", "unchanged", "changed_in", "diff_only"}
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_]\w*\}")

# the subcall triple (spawn -> _subcall marker -> poll).
_RUNNER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ARTIFACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# a scenario names a FILE in the recipes dir and the child run_id prefix:
# no separators, no leading dot (mirrors the engine's own guard).
_SCENARIO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # no dots: dotted-path resolution
_SPAWN_EDGE_COND_RE = re.compile(r"^verdict_status == '(pass|fail)'$")
_POLL_EDGE_COND_RE = re.compile(r"^_subcall_status == '(running|done|error)'$")
_HASH_FROM_RE = re.compile(r"^_subcall_envelope\.artifact_hashes\.([a-z][a-z0-9_-]*)$")


def _is_subcall_marker(message: Any) -> bool:
    return isinstance(message, dict) and message.get("step") == "_subcall"


def subcall_node_kind(node: Any, tools: dict) -> str | None:
    """Classify by RESOLVED module/function, never by tool NAME: a
    recipe aliasing `my_spawn: {module: lockstep_mcp.subcalls, function:
    spawn}` must hit every subcall rule. Shared with Engine._predict_spawn."""
    if not isinstance(node, dict) or node.get("type") != "python":
        return None
    tool_cfg = tools.get(node.get("tool")) if isinstance(tools, dict) else None
    if not isinstance(tool_cfg, dict) or tool_cfg.get("module") != "lockstep_mcp.subcalls":
        return None
    fn = tool_cfg.get("function")
    return fn if fn in ("spawn", "poll") else None


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
    seen_sources: set[str] = set()
    for src, tgt in _find_back_edges(edges_by_from):
        tgt_node = nodes.get(tgt) or {}
        if tgt_node.get("type") == "interrupt" and _is_subcall_marker(tgt_node.get("message") or {}):
            continue  # poll loop: termination is the runner timeout, not loop_limits
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

    # Subcall markers (third interrupt class) are exempt from the work rules
    # below — task/exit_criterion/>=1 check and validator pairing; their own
    # rules live in _check_subcall_rules. They are NOT exempt from the
    # idempotent check above or the checks:/placeholder scan further down —
    # marker checks are never executed, but a command_from smuggled onto one
    # must still be refused where v1 scans every interrupt.
    if not _is_escalate_marker(message) and not _is_subcall_marker(message):
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


def _check_subcall_rules(doc: dict, nodes: dict, edges_by_from: dict,
                         child_dir: Path, errors: list[str]) -> None:
    tools = doc.get("tools") or {}
    markers = {n: cfg for n, cfg in nodes.items()
               if isinstance(cfg, dict) and cfg.get("type") == "interrupt"
               and _is_subcall_marker(cfg.get("message") or {})}
    spawns = {n for n, cfg in nodes.items() if subcall_node_kind(cfg, tools) == "spawn"}
    polls = {n for n, cfg in nodes.items() if subcall_node_kind(cfg, tools) == "poll"}

    # hash_from is scanned even when the recipe has NO subcall nodes: the
    # rule is "every hash_from in the recipe", and a hash_from check in a
    # subcall-free recipe names an artifact nothing can ever declare.
    hash_names: set[str] = set()
    for cfg in nodes.values():
        msg = cfg.get("message") if isinstance(cfg, dict) else None
        if not isinstance(msg, dict):
            continue  # a bare-string message is reported by the interrupt rules
        for check in (msg.get("checks") or []):
            hf = check.get("hash_from") if isinstance(check, dict) else None
            if hf is None:
                continue
            m = _HASH_FROM_RE.fullmatch(hf) if isinstance(hf, str) else None
            if m is None:
                errors.append("hash_from must match "
                              f"_subcall_envelope.artifact_hashes.<name> (dot-free name): {hf!r}")
            else:
                hash_names.add(m.group(1))

    declared_artifacts: set[str] = set()
    if markers or spawns or polls:
        state = doc.get("state") or {}
        if "_subcall_status" not in state or "_subcall_envelope" not in state:
            errors.append("recipe uses subcalls but does not declare "
                          "_subcall_status/_subcall_envelope in state:")
        edges_by_to: dict[str, list[dict]] = {}
        for src, elist in edges_by_from.items():
            for e in elist:
                edges_by_to.setdefault(e.get("to"), []).append(e)
        # validators DIRECTLY PAIRED with a work interrupt: the single
        # edge-target of some non-marker, non-escalate interrupt.
        paired_validators: set[str] = set()
        for n, cfg in nodes.items():
            if not isinstance(cfg, dict) or cfg.get("type") != "interrupt":
                continue
            msg = cfg.get("message") or {}
            if _is_escalate_marker(msg) or _is_subcall_marker(msg):
                continue
            targets = {e.get("to") for e in edges_by_from.get(n, [])}
            if len(targets) == 1:
                paired_validators.add(next(iter(targets)))

        seen_node_ids: set[str] = set()
        for mname, mcfg in markers.items():
            msg = mcfg.get("message") or {}
            node_id = msg.get("node")
            if not isinstance(node_id, str) or not _ARTIFACT_NAME_RE.fullmatch(node_id or ""):
                errors.append(f"subcall marker '{mname}': node must match ^[a-z][a-z0-9_-]*$")
            elif node_id in seen_node_ids:
                errors.append(f"subcall marker '{mname}': duplicate node id '{node_id}' — "
                              "workdirs and the single-start claim are keyed on it")
            else:
                seen_node_ids.add(node_id)
            runner = msg.get("runner")
            if runner is not None and (not isinstance(runner, str)
                                       or not _RUNNER_NAME_RE.fullmatch(runner)):
                errors.append(f"subcall marker '{mname}': runner name must match ^[a-z][a-z0-9-]*$")
            prompt = msg.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append(f"subcall marker '{mname}': prompt is required (non-empty string)")
            elif PLACEHOLDER_RE.search(prompt):
                errors.append(f"placeholder found in subcall prompt (marker '{mname}'): "
                              "prompts are verbatim, vars never reach them")
            tm = msg.get("timeout_minutes")
            if tm is not None and (not isinstance(tm, int) or isinstance(tm, bool) or tm <= 0):
                errors.append(f"subcall marker '{mname}': timeout must be a "
                              "positive number of minutes")
            if not any(e.get("from") in spawns for e in edges_by_to.get(mname, [])):
                errors.append(f"subcall marker '{mname}' has no spawn node feeding it")
            out = edges_by_from.get(mname, [])
            if len(out) != 1 or out[0].get("to") not in polls or out[0].get("condition"):
                errors.append(f"subcall marker '{mname}' has no poll node (exactly one "
                              "unconditional edge to a subcall poll node required)")
            scenario = msg.get("scenario")
            artifacts = msg.get("artifacts")
            if artifacts is not None and not scenario:
                errors.append(f"subcall marker '{mname}': artifacts requires scenario "
                              "(one-shot subcalls pin no project artifacts — the envelope is the artifact)")
            if scenario is not None and not (
                isinstance(scenario, str) and _SCENARIO_NAME_RE.fullmatch(scenario)
            ):
                errors.append(f"subcall marker '{mname}': scenario name {scenario!r} must be a "
                              "plain file name (letters, digits, dot, dash, underscore)")
            elif scenario is not None:
                child_path = child_dir / f"{scenario}.yaml"
                if not child_path.exists():
                    errors.append(f"fractal child recipe '{scenario}' not found in {child_dir}")
                else:
                    child_doc = yaml.safe_load(child_path.read_text()) or {}
                    child_globs = list(child_doc.get("baseline_globs") or [])
                    for aname, apath in (artifacts or {}).items():
                        if not isinstance(aname, str) or not _ARTIFACT_NAME_RE.fullmatch(aname):
                            errors.append(f"subcall marker '{mname}': artifact name {aname!r} "
                                          "must match ^[a-z][a-z0-9_-]*$ (no dots)")
                            continue
                        declared_artifacts.add(aname)
                        if (not isinstance(apath, str) or not apath
                                or apath.startswith("/") or ".." in apath.split("/")):
                            errors.append(f"subcall marker '{mname}': artifact '{aname}' path "
                                          "must be a relative project path")
                            continue
                        if not _path_covered(apath, child_globs):
                            errors.append(f"child recipe '{scenario}' baseline_globs do not "
                                          f"cover artifact '{aname}' ({apath})")

        for pname in sorted(polls):
            pedges = edges_by_from.get(pname, [])
            if not any(e.get("to") in markers
                       and (e.get("condition") or "").strip() == "_subcall_status == 'running'"
                       for e in pedges):
                errors.append(f"poll node '{pname}': a back edge to its marker conditioned "
                              "exactly \"_subcall_status == 'running'\" is required")
            for e in pedges:
                cond = (e.get("condition") or "").strip()
                if not _POLL_EDGE_COND_RE.fullmatch(cond):
                    errors.append(f"poll node '{pname}': every outgoing edge must be "
                                  f"conditioned on _subcall_status equality only "
                                  f"(edge to {e.get('to')!r})")

        for sname in sorted(spawns):
            s_out = edges_by_from.get(sname, [])
            if len(s_out) != 1 or s_out[0].get("to") not in markers or s_out[0].get("condition"):
                errors.append(f"spawn node '{sname}' must have exactly one unconditional "
                              "edge to its subcall marker")
            for e in edges_by_to.get(sname, []):
                src, cond = e.get("from"), (e.get("condition") or "").strip()
                if src == "START":
                    # a START -> spawn edge is forbidden.
                    # It would fire the spawn hook on an EMPTY evidence
                    # channel (guaranteed error envelope) and bypass the
                    # engine's done()-time policy prediction — the only
                    # point where runner/budget/depth are checked.
                    errors.append(f"spawn node '{sname}' must not be entered from START — "
                                  "a spawn must follow a validator (a start-time spawn has "
                                  "no evidence ctx and bypasses subcall policy prediction)")
                    continue
                if src not in paired_validators:
                    errors.append(f"spawn node '{sname}' must be a direct conditional successor "
                                  f"of a validator or START (edge from {src!r})")
                if not _SPAWN_EDGE_COND_RE.fullmatch(cond):
                    errors.append(f"edge into spawn node '{sname}' must be conditioned on "
                                  "verdict_status equality only")

    for name in sorted(hash_names - declared_artifacts):
        errors.append(f"hash_from names artifact '{name}' but no subcall marker "
                      "declares it in artifacts:")


def check_recipe_full(
    path: str | Path, *, child_recipes_dir: str | Path | None = None
) -> tuple[list[str], list[str]]:
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

    # work-interrupt `step` names must be UNIQUE across the recipe —
    # `Engine._predict_spawn` and `done(run_id, step, ...)` both key on the
    # parked step name; a collision makes the prediction read the wrong
    # validator (an orphan child holding a live credential, or a spawn with
    # no ctx). Escalate markers (all `escalate`) and subcall markers (all
    # `_subcall`) share their step by construction and are exempt.
    seen_steps: dict[str, str] = {}
    for name, node in nodes.items():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        msg = node.get("message") or {}
        if not isinstance(msg, dict):
            continue  # reported by the interrupt rules
        if _is_escalate_marker(msg) or _is_subcall_marker(msg):
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
        _check_interrupt_node(name, node, edges_by_from, nodes, doc, errors)

    _check_loops(
        edges_by_from,
        nodes,
        doc.get("loop_limits") or {},
        doc.get("loop_exits") or {},
        errors,
    )

    # Fractal children resolve beside the checked file by default; the
    # engine passes its recipes_dir because it profiles a staging copy
    # inside state_dir/runs/ where "beside the recipe" resolves to nothing.
    child_dir = Path(child_recipes_dir) if child_recipes_dir else Path(path).parent
    _check_subcall_rules(doc, nodes, edges_by_from, child_dir, errors)

    for tname, tcfg in (doc.get("tools") or {}).items():
        module = tcfg.get("module") if isinstance(tcfg, dict) else None
        if module and not module.startswith("lockstep_mcp."):
            warnings.append(
                f"local tools.py: tool '{tname}' references module '{module}' outside "
                "lockstep_mcp — human review recommended"
            )

    return errors, warnings


def check_recipe(
    path: str | Path, *, child_recipes_dir: str | Path | None = None
) -> list[str]:
    errors, _warnings = check_recipe_full(path, child_recipes_dir=child_recipes_dir)
    return errors
