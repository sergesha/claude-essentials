"""Task 5: `Engine` — the run manager. Composes Tasks 1-4 (`yamlgraph_api`,
`validators`, `evidence`, `runs`) into the durable start/status/done/
escalate/abort surface the MCP server (Task 6) delegates to.

Mechanics implemented here (see the plan's Task 5 "Mechanics fixed by
reviews" paragraph for the authoritative spec):

- **Substitution whitelist** (decision 4): `_subst` rewrites `{var}`
  placeholders from run vars into `task`/`exit_criterion` ONLY. `checks`/
  `evidence_schema` are carried verbatim from the recipe snapshot — the
  profile (Task 3) already refuses any recipe with a placeholder in either,
  so nothing here ever substitutes them.
- **Recipe snapshot** (decision 8): `start()` copies recipe bytes to
  `runs/<run-id>.recipe.yaml` and validates (profile + `cli_validate`) ONLY
  that snapshot; `_app()` recompiles from the snapshot path for the run's
  entire lifetime, so a live mid-run edit to the source recipe is inert.
  Validation happens on a staging copy BEFORE the run is ever registered in
  the index, so a bad recipe never creates a half-alive run.
- **Baseline lifecycle** (decision 14): `start()` writes the immutable
  run-start manifest (`runs/<id>.baseline.json`, the `_baseline_start` ctx
  key forever) plus `baseline.0.json` (step 1's `_baseline_prev`). Every
  PASS (including the final one) writes `baseline.<n+1>.json` and bumps a
  small counter file that tracks "the latest passed-step snapshot" — this
  survives across Engine instances because it is a file, not in-memory
  state. The four ctx keys are passed inside the `state` dict handed
  straight to `validators.run_checks(state, execute=True)` — never as
  graph state (review-3 M6).
- **Anti-forgery + single check execution** (decision 16): `done()` rejects
  any raw evidence key starting with `_` BEFORE schema validation, then
  runs the step's checks exactly once via the engine's own direct
  `run_checks(state, execute=True)` call. `error` verdicts never resume
  the graph (loop budget untouched by construction); `pass`/`fail` resume
  with the verdict embedded in the payload (`{**evidence, "_verdict_status":
  ..., "_verdict_reasons": ...}`) so the in-graph node only republishes.
- **Path handling** (decision 12): validate raw evidence against schema →
  resolve every `format: project-path` property against `run.project` →
  reject any resolved path escaping the project root — all BEFORE checks
  ever run. `validators.py` re-resolves/re-contains independently (belt).
- **Escalate is terminal** (decision 5): both loop-exhaustion (the graph
  parks on the `{step: escalate}` marker after a `fail` resume) and an
  explicit `escalate()` call flip the run to terminal `escalated`; no
  resume path exists afterward. `abort()` is the same shape for `aborted`.
- **Write order + `_reconcile`** (decision 13): a transition writes the
  graph checkpoint first (`yamlgraph_api.resume`), then the index second.
  `_reconcile`, called from `status()`/`done()`, reads the checkpoint via
  `yamlgraph_api.peek()` (Task 5 addition) and repairs the index on
  disagreement — EXCEPT it never overrides a terminal index status
  (`runs.TERMINAL_STATUSES`, the single definition of terminal).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lockstep_mcp import evidence as evidence_mod
from lockstep_mcp import profile_check
from lockstep_mcp import runners
from lockstep_mcp import subcalls
from lockstep_mcp import validators
from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.runs import TERMINAL_STATUSES, RunIndex, RunRecord

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

# C1: `start()`'s vars dict is passed straight into the initial LangGraph
# state (`yg.start(self._app(run_id), dict(vars), run_id)`). Any key that
# collides with a graph-internal state key (`brief`/`evidence`/
# `verdict_status`/`verdict_reasons`) or starts with `_` (yamlgraph's own
# internal bookkeeping, e.g. `_loop_counts`) must never reach that call —
# a hostile var could otherwise overwrite the parked brief, forge a
# verdict, or pre-seed/reset the loop-limit counter and defeat
# `loop_limits` entirely. Rejected at `start()`, before any run is created.
_RESERVED_VAR_KEYS = {"brief", "evidence", "verdict_status", "verdict_reasons"}


class LockstepError(Exception):
    """Raised for terminal-run violations, wrong-step calls, and recipes
    that fail validation at `start()`."""


def _subst(text: str, vars_: dict) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(vars_[key]) if key in vars_ else m.group(0)

    return _PLACEHOLDER_RE.sub(repl, text or "")


class Engine:
    def __init__(self, state_dir: Path, recipes_dir: Path, memory_only: bool = False) -> None:
        self._state_dir = Path(state_dir)
        self._recipes_dir = Path(recipes_dir)
        self._memory_only = memory_only
        self._runs = RunIndex(self._state_dir)
        # memory_only has no durable checkpoint file to recompile from, so
        # the SAME app object (and its in-process MemorySaver) must be
        # reused across calls within one Engine instance's lifetime.
        self._apps: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def recipe_path(self, name: str) -> Path:
        return self._recipes_dir / f"{name}.yaml"

    def _runs_dir(self) -> Path:
        d = self._state_dir / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot_path(self, run_id: str) -> Path:
        return self._runs_dir() / f"{run_id}.recipe.yaml"

    def _child_snapshot_path(self, run_id: str, scenario: str) -> Path:
        """C2: the pinned copy of a fractal child recipe, taken when THIS run
        started. `_start_child` launches from it — never from the live,
        agent-writable recipes dir — so a mid-run edit to a child recipe is
        as inert as one to the parent's own recipe."""
        return self._runs_dir() / f"{run_id}.child.{scenario}.yaml"

    @staticmethod
    def _marker_messages(doc: dict) -> list[dict]:
        out = []
        for cfg in (doc.get("nodes") or {}).values():
            msg = cfg.get("message") if isinstance(cfg, dict) else None
            if isinstance(msg, dict) and msg.get("step") == "_subcall":
                out.append(msg)
        return out

    def _vars_path(self, run_id: str) -> Path:
        return self._runs_dir() / f"{run_id}.vars.json"

    def _baseline_start_path(self, run_id: str) -> Path:
        return self._runs_dir() / f"{run_id}.baseline.json"

    def _baseline_n_path(self, run_id: str, n: int) -> Path:
        return self._runs_dir() / f"{run_id}.baseline.{n}.json"

    def _baseline_counter_path(self, run_id: str) -> Path:
        return self._runs_dir() / f"{run_id}.baseline_index"

    def route_log_path(self, run_id: str) -> Path:
        return self._runs_dir() / f"{run_id}.route.jsonl"

    def _log_transition(self, run_id: str, from_step: str, verdict: str, to_step: str | None) -> None:
        """Route-log fallback (Task 1 probe, spike M7): yamlgraph exposes no
        per-run route-log hook, so the engine witnesses its own completed
        `done()` transitions here — best-effort, JSONL, failures ignored.

        M1: `event` must be `"route"` with `node`/`target`/`thread_id` —
        yamlgraph's own `parse_route_lines` (consumed by `render_flow`'s
        `--overlay`, see `yamlgraph_api.cli_mermaid`) only picks up objects
        matching its own frozen grammar (`yamlgraph.utils.route_log`
        module docstring): `{"event":"route","node":...,"value":...,
        "target":...,"thread_id":...}`. `render_overlay` doesn't require
        `node`/`target` to name actual authored graph nodes — an edge it
        doesn't recognize is added as a synthetic, ordinal-marked one — so
        our step-level names (not yamlgraph's internal node ids) still
        render real overlay evidence. Extra fields below are additive and
        harmless to that parser (it reads only the four route keys).
        """
        entry = {
            "event": "route",
            "node": from_step,
            "value": verdict,
            "target": to_step if to_step is not None else "END",
            "thread_id": run_id,
            # kept for our own diagnostics; not part of yamlgraph's grammar
            "run": run_id,
            "from": from_step,
            "verdict": verdict,
            "to": to_step,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(self.route_log_path(run_id), "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:  # noqa: BLE001 - best-effort, never blocks a transition
            pass

    # ------------------------------------------------------------------
    # small persisted-state helpers
    # ------------------------------------------------------------------

    def _read_vars(self, run_id: str) -> dict:
        p = self._vars_path(run_id)
        if not p.exists():
            return {}
        return json.loads(p.read_text())

    def _read_baseline_globs(self, snapshot_path: Path) -> list[str]:
        with open(snapshot_path) as f:
            doc = yaml.safe_load(f) or {}
        return list(doc.get("baseline_globs") or [])

    def _read_baseline_counter(self, run_id: str) -> int:
        p = self._baseline_counter_path(run_id)
        if not p.exists():
            return 0
        text = p.read_text().strip()
        return int(text) if text else 0

    def _current_prev_baseline_path(self, run_id: str) -> Path:
        return self._baseline_n_path(run_id, self._read_baseline_counter(run_id))

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data))

    def _advance_baseline(self, run_id: str, project: str, globs: list[str]) -> None:
        counter = self._read_baseline_counter(run_id) + 1
        manifest = validators.build_manifest(Path(project), globs)
        self._write_json(self._baseline_n_path(run_id, counter), manifest)
        self._baseline_counter_path(run_id).write_text(str(counter))

    # ------------------------------------------------------------------
    # brief substitution
    # ------------------------------------------------------------------

    def _substitute_brief(self, brief: yg.StepBrief, vars_: dict) -> yg.StepBrief:
        return yg.StepBrief(
            step=brief.step,
            task=_subst(brief.task, vars_),
            exit_criterion=_subst(brief.exit_criterion, vars_),
            evidence_schema=brief.evidence_schema,
            checks=brief.checks,
            raw=brief.raw,
        )

    def _brief_to_dict(self, brief: yg.StepBrief) -> dict:
        return {
            "step": brief.step,
            "task": brief.task,
            "exit_criterion": brief.exit_criterion,
            "evidence_schema": brief.evidence_schema,
            "checks": brief.checks,
        }

    # ------------------------------------------------------------------
    # compiled-app access
    # ------------------------------------------------------------------

    def _app(self, run_id: str):
        if self._memory_only:
            app = self._apps.get(run_id)
            if app is None:
                app = yg.compile_recipe(self._snapshot_path(run_id), db_path=None)
                self._apps[run_id] = app
            return app
        return yg.compile_recipe(self._snapshot_path(run_id), db_path=self._runs.db_path(run_id))

    # ------------------------------------------------------------------
    # decision 13: checkpoint-first write order + repair
    # ------------------------------------------------------------------

    def _reconcile(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if record.status in TERMINAL_STATUSES:
            # ONE definition of terminal (runs.TERMINAL_STATUSES) — a
            # terminal index status is never overridden: escalated/aborted
            # have no checkpoint counterpart to disagree with, and a done
            # record must not regain a live step+brief from a checkpoint.
            return record

        try:
            adv = yg.peek(self._app(run_id), run_id)
        except Exception:  # noqa: BLE001 - reconcile is best-effort; index truth stands
            return record

        if adv.done:
            if record.status != "done":
                record = self._runs.update(run_id, status="done", brief=None)
                self._cascade_terminate(run_id)
            return record

        if adv.brief is None:
            return record

        if adv.brief.step == "escalate":
            if record.status != "escalated":
                record = self._runs.update(
                    run_id,
                    status="escalated",
                    step="escalate",
                    brief={"step": "escalate", "reason": "loop limit reached"},
                )
                self._cascade_terminate(run_id)
            return record

        if adv.brief.step == "_subcall":
            # Persist the FULL marker as the index brief — but only if the
            # index isn't already parked on it (preserve an existing
            # `started_at`). A repair that never saw the park writes the
            # marker without `started_at` — the "?m" path.
            if record.status != "awaiting" or (record.brief or {}).get("step") != "_subcall":
                record = self._runs.update(
                    run_id, status="awaiting", step="_subcall", brief=dict(adv.brief.raw)
                )
            return record

        if record.status != "awaiting" or adv.brief.step != record.step or record.brief is None:
            vars_ = self._read_vars(run_id)
            substituted = self._substitute_brief(adv.brief, vars_)
            record = self._runs.update(
                run_id,
                status="awaiting",
                step=substituted.step,
                brief=self._brief_to_dict(substituted),
            )
        return record

    # ------------------------------------------------------------------
    # decision 12: evidence path containment
    # ------------------------------------------------------------------

    def _check_path_containment(self, schema: Any, evidence: dict, project: str) -> list[str]:
        if not isinstance(schema, dict):
            return []
        props = schema.get("properties") or {}
        base = Path(project).resolve()
        errors: list[str] = []
        for key, prop in props.items():
            if not isinstance(prop, dict) or prop.get("format") != "project-path":
                continue
            if key not in evidence:
                continue
            raw = evidence[key]
            if not isinstance(raw, str):
                errors.append(f"{key}: project-path value must be a string")
                continue
            resolved = (base / raw).resolve()
            if resolved != base and base not in resolved.parents:
                errors.append(f"{key}: path escapes project root: {raw!r}")
        return errors

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    def start(self, recipe: str, vars: dict, project: str) -> dict:  # noqa: A002 - frozen name
        # The state dir (runners.yaml, runs.json) is the trust anchor and
        # LOCKSTEP_STATE_DIR arrives unvalidated from the environment —
        # refuse one placed inside the agent-writable project tree.
        runners.assert_state_dir_sane(self._state_dir, Path(project))
        vars = vars or {}
        hostile = sorted(k for k in vars if k.startswith("_") or k in _RESERVED_VAR_KEYS)
        if hostile:
            raise LockstepError(f"reserved/hostile var key(s) rejected: {hostile}")
        return self._launch(recipe, vars, project)

    def _launch(self, recipe: str, vars: dict, project: str,  # noqa: A002 - mirrors start()
                parent_run: str | None = None, nonce: str | None = None,
                src: Path | None = None) -> dict:
        """m7.8: start()'s body after the hostile-var check, extracted so a
        fractal child run launches through the exact same staging + profile
        + cli_validate + baseline path. `RunIndex.create` is the ONLY writer
        of parent_run/nonce (immutability holds). `src` overrides recipe-name
        resolution — `_start_child` passes the parent's PINNED child copy so
        the child never launches from the live recipes dir (C2)."""
        vars = vars or {}  # noqa: A001 - mirrors start()
        src = src if src is not None else self.recipe_path(recipe)
        if not src.exists():
            raise LockstepError(f"recipe not found: {recipe}")
        raw_bytes = src.read_bytes()

        runs_dir = self._runs_dir()
        staging = runs_dir / f".staging-{uuid.uuid4().hex}.yaml"
        staging.write_bytes(raw_bytes)
        # C2: fractal child recipes are copied out of the agent-writable
        # recipes dir ONCE, here, at this run's start. The profile check runs
        # against these staged copies (no check-then-act on a name the worker
        # owns), and on success they become runs/<id>.child.<scenario>.yaml —
        # the ONLY source `_start_child` will launch from.
        doc = yaml.safe_load(raw_bytes) or {}
        scenarios = sorted({m["scenario"] for m in self._marker_messages(doc)
                            if isinstance(m.get("scenario"), str) and m["scenario"]})
        child_staging = runs_dir / f".staging-children-{uuid.uuid4().hex}"
        try:
            child_staging.mkdir()
            for scenario in scenarios:
                live = self._recipes_dir / f"{scenario}.yaml"
                if live.exists():
                    (child_staging / f"{scenario}.yaml").write_bytes(live.read_bytes())
            # child_recipes_dir: the staging copy lives in state_dir/runs/,
            # where "beside the recipe" resolves to nothing.
            profile_errors = profile_check.check_recipe(
                staging, child_recipes_dir=child_staging
            )
            if profile_errors:
                raise LockstepError(
                    f"recipe {recipe!r} failed profile check: " + "; ".join(profile_errors)
                )
            ok, msg = yg.cli_validate(staging)
            if not ok:
                raise LockstepError(f"recipe {recipe!r} failed to compile: {msg}")

            # I1: loud START-time refusal for an unlisted runner, a relative/
            # non-executable path, or an empty models allowlist — never N steps
            # of real work followed by a wedge at the first spawn-bearing gate.
            # Budgets/depth stay done()-time (they depend on runtime state);
            # this resolve also re-runs there, so a mid-run runners.yaml
            # removal is still refused at the gate.
            for m in self._marker_messages(doc):
                try:
                    runners.resolve(self._state_dir, m.get("runner"), os.environ)
                except runners.RunnerError as exc:
                    raise LockstepError(
                        f"recipe {recipe!r}, subcall marker "
                        f"'{m.get('node')}': runner unavailable at start: {exc}"
                    ) from exc

            record = self._runs.create(recipe, project, parent_run=parent_run, nonce=nonce)
            staging.replace(self._snapshot_path(record.run_id))
            for scenario in scenarios:
                staged = child_staging / f"{scenario}.yaml"
                if staged.exists():
                    staged.replace(self._child_snapshot_path(record.run_id, scenario))
        finally:
            if staging.exists():
                staging.unlink()
            shutil.rmtree(child_staging, ignore_errors=True)

        run_id = record.run_id
        self._vars_path(run_id).write_text(json.dumps(vars))

        project_root = Path(project).resolve()
        globs = self._read_baseline_globs(self._snapshot_path(run_id))
        start_manifest = validators.build_manifest(project_root, globs)
        self._write_json(self._baseline_start_path(run_id), start_manifest)
        self._write_json(self._baseline_n_path(run_id, 0), start_manifest)

        adv = yg.start(self._app(run_id), dict(vars), run_id)
        if adv.done or adv.brief is None:
            raise LockstepError(f"recipe {recipe!r} produced no work step at start")

        substituted = self._substitute_brief(adv.brief, vars)
        self._runs.update(run_id, step=substituted.step, brief=self._brief_to_dict(substituted))

        return {
            "run_id": run_id,
            "step": substituted.step,
            "task": substituted.task,
            "exit_criterion": substituted.exit_criterion,
            "evidence_schema": substituted.evidence_schema,
        }

    def _reconcile_polling(self, run_id: str) -> RunRecord:
        """Entry head for status/done/escalate/abort: reconcile, and if the
        run is parked in a subcall, auto-poll it ONCE. The caller decides
        what a still-parked run means (refuse / raise / report)."""
        record = self._reconcile(run_id)
        if self._is_subcall_parked(record):
            record = self._auto_poll(run_id)
        return record

    def status(self, run_id: str) -> dict:
        record = self._reconcile_polling(run_id)
        if self._is_subcall_parked(record):
            marker = record.brief or {}
            started = marker.get("started_at")
            return {
                "status": record.status,
                "recipe": record.recipe,
                "run_id": record.run_id,
                "step": record.step,
                "task": marker.get("task"),
                "exit_criterion": marker.get("exit_criterion"),
                "evidence_schema": marker.get("evidence_schema"),
                "last_fail_reasons": marker.get("last_fail_reasons"),
                "subcall": {
                    "node": marker.get("node"),
                    "runner": marker.get("runner"),
                    "running_minutes": int((time.time() - float(started)) // 60)
                    if started
                    else None,
                },
            }
        if record.status != "awaiting":
            return {"status": record.status, "recipe": record.recipe, "step": record.step}
        brief = record.brief or {}
        return {
            "status": record.status,
            "recipe": record.recipe,
            "run_id": record.run_id,
            "step": record.step,
            "task": brief.get("task"),
            "exit_criterion": brief.get("exit_criterion"),
            "evidence_schema": brief.get("evidence_schema"),
            "last_fail_reasons": brief.get("last_fail_reasons"),
        }

    def done(self, run_id: str, step: str, evidence: dict) -> dict:
        # Auto-poll BEFORE the terminal raise, so a subcall that just
        # resolved reports its accurate terminal status.
        record = self._reconcile_polling(run_id)
        if self._is_subcall_parked(record):
            marker = record.brief or {}
            started = marker.get("started_at")
            mins = f"{int((time.time() - float(started)) // 60)}m" if started else "?m"
            return {"accepted": False, "errors": [
                f"subcall in progress: {marker.get('node')} ({marker.get('runner')}), {mins}"]}
        if record.status != "awaiting":
            raise LockstepError(f"run {run_id} is {record.status} — terminal")
        if record.step != step:
            raise LockstepError(
                f"run {run_id} is parked on step {record.step!r}, not {step!r}"
            )

        raw_evidence = evidence or {}

        # anti-forgery: reserved `_` prefix rejected BEFORE schema validation.
        forged = [k for k in raw_evidence if k.startswith("_")]
        if forged:
            return {
                "accepted": False,
                "errors": [f"reserved evidence key(s) rejected: {sorted(forged)}"],
            }

        brief_dict = record.brief or {}
        schema = brief_dict.get("evidence_schema")

        schema_errors = evidence_mod.validate_evidence(schema, raw_evidence)
        if schema_errors:
            return {"accepted": False, "errors": schema_errors}

        path_errors = self._check_path_containment(schema, raw_evidence, record.project)
        if path_errors:
            return {"accepted": False, "errors": path_errors}

        globs = self._read_baseline_globs(self._snapshot_path(run_id))
        state = {
            "brief": brief_dict,
            "evidence": raw_evidence,
            "_project": record.project,
            "_baseline_start": str(self._baseline_start_path(run_id)),
            "_baseline_prev": str(self._current_prev_baseline_path(run_id)),
            "_baseline_globs": globs,
            "_state": self._peek_state(run_id),
        }
        verdict = validators.run_checks(state, execute=True)
        vstatus = verdict["verdict_status"]
        reasons = list(verdict.get("verdict_reasons") or [])

        if vstatus == "error":
            # decision 16: never resume; loop budget untouched by
            # construction (no yamlgraph_api.resume call on this path).
            return {
                "accepted": True,
                "passed": False,
                "error": True,
                "reasons": reasons,
                "step": step,
            }

        # Pre-resume policy prediction: if this verdict would route into a
        # spawn node, refuse (unknown runner / budget / depth) ENGINE-SIDE
        # before any resume — the v1 no-resume path, loop budget untouched.
        spawn_node = self._predict_spawn(run_id, vstatus)
        ctx: dict = {}
        if spawn_node is not None:
            policy_error = self._check_subcall_policy(run_id, record, spawn_node)
            if policy_error is not None:
                return {"accepted": True, "passed": False, "error": True,
                        "reasons": [policy_error], "step": step}
            try:
                ctx = self._subcall_ctx(run_id, record, spawn_node)
            except (runners.RunnerError, LockstepError) as exc:
                return {"accepted": True, "passed": False, "error": True,
                        "reasons": [str(exc)], "step": step}

        # The ctx keys are `_`-prefixed, so the worker can never smuggle them
        # in raw evidence (rejected above). NOTE, stated so nobody "optimizes"
        # it away: the spawn payload (including `_subcall_env` with the child
        # nonce) persists in the checkpoint's `evidence` channel until the
        # FIRST poll tick replaces it — the slim tick (`_poll_ctx`) is what
        # keeps the credential out of every later `_peek_state`/check
        # `_state`. The checkpoint db lives inside the denied state dir;
        # tolerable, not gratuitous.
        resume_payload = {**raw_evidence, "_verdict_status": vstatus,
                          "_verdict_reasons": reasons, **ctx}
        adv = yg.resume(self._app(run_id), resume_payload, run_id)

        if vstatus == "fail":
            if not adv.done and adv.brief is not None and adv.brief.step == "escalate":
                self._runs.update(
                    run_id,
                    status="escalated",
                    step="escalate",
                    brief={"step": "escalate", "reason": "; ".join(reasons) or "loop limit reached"},
                )
                self._cascade_terminate(run_id)
                self._log_transition(run_id, step, "fail", "escalate")
                return {
                    "accepted": True,
                    "passed": False,
                    "reasons": reasons,
                    "step": step,
                    "escalated": True,
                }

            parked = self._maybe_park_subcall(run_id, step, "fail", adv)
            if parked is not None:
                return parked

            vars_ = self._read_vars(run_id)
            substituted = self._substitute_brief(adv.brief, vars_)
            # item 11: persist the fail reasons into the parked brief so a
            # restart (fresh Engine, `status()` reading only runs.json)
            # still surfaces WHY the last attempt failed, not just that a
            # step is awaiting retry.
            repeated_brief = self._brief_to_dict(substituted)
            repeated_brief["last_fail_reasons"] = reasons
            self._runs.update(run_id, step=substituted.step, brief=repeated_brief)
            self._log_transition(run_id, step, "fail", substituted.step)
            return {"accepted": True, "passed": False, "reasons": reasons, "step": step}

        # vstatus == "pass"

        # C2: yamlgraph's loop guard runs BEFORE the validator node
        # executes (see yamlgraph_api.py module docstring, "loop_limits /
        # loop_exits" probe). A step whose checks PASS on what would be its
        # (limit+1)th validator execution never gets that execution: the
        # guard trips first and routes straight to the escalate marker,
        # same as a `fail` at the cap — regardless of the passing verdict
        # this engine already computed. Report that honestly instead of the
        # generic "passed" shape: the run IS terminal, and the baseline
        # must NOT advance (this was not a real accepted step).
        if not adv.done and adv.brief is not None and adv.brief.step == "escalate":
            note = "loop cap reached; work validated but the run requires human review"
            self._runs.update(
                run_id,
                status="escalated",
                step="escalate",
                brief={"step": "escalate", "reason": note},
            )
            self._cascade_terminate(run_id)
            self._log_transition(run_id, step, "pass", "escalate")
            return {
                "accepted": True,
                "passed": True,
                "escalated": True,
                "step": "escalate",
                "done": False,
                "reasons": [note],
            }

        self._advance_baseline(run_id, record.project, globs)

        parked = self._maybe_park_subcall(run_id, step, "pass", adv)
        if parked is not None:
            return parked

        if adv.done:
            self._runs.update(run_id, status="done", step=step, brief=None)
            self._cascade_terminate(run_id)
            self._log_transition(run_id, step, "pass", None)
            return {
                "accepted": True,
                "passed": True,
                "step": None,
                "done": True,
                "task": None,
                "exit_criterion": None,
            }

        vars_ = self._read_vars(run_id)
        substituted = self._substitute_brief(adv.brief, vars_)
        self._runs.update(run_id, step=substituted.step, brief=self._brief_to_dict(substituted))
        self._log_transition(run_id, step, "pass", substituted.step)
        return {
            "accepted": True,
            "passed": True,
            "step": substituted.step,
            "done": False,
            "task": substituted.task,
            "exit_criterion": substituted.exit_criterion,
        }

    def _raise_if_subcall_parked(self, record: RunRecord) -> None:
        # A poisoned child must not kill its parent; the finite runner
        # timeout is the escape.
        if self._is_subcall_parked(record):
            marker = record.brief or {}
            raise LockstepError(
                f"subcall in progress: {marker.get('node')} ({marker.get('runner')}) — "
                "abort/escalate unavailable until it completes or times out")

    def escalate(self, run_id: str, reason: str) -> dict:
        record = self._reconcile_polling(run_id)
        self._raise_if_subcall_parked(record)
        if record.status != "awaiting":
            raise LockstepError(f"run {run_id} is {record.status} — terminal")
        self._runs.update(
            run_id,
            status="escalated",
            step="escalate",
            brief={"step": "escalate", "reason": reason},
        )
        self._cascade_terminate(run_id)
        return {"run_id": run_id, "status": "escalated", "reason": reason}

    def abort(self, run_id: str) -> dict:
        record = self._reconcile_polling(run_id)
        self._raise_if_subcall_parked(record)
        if record.status != "awaiting":
            raise LockstepError(f"run {run_id} is {record.status} — terminal")
        self._runs.update(run_id, status="aborted")
        self._cascade_terminate(run_id)
        return {"run_id": run_id, "status": "aborted"}

    # ------------------------------------------------------------------
    # v2 subcalls: auto-poll, park persistence, pre-resume prediction
    # ------------------------------------------------------------------

    def _snapshot_doc(self, run_id: str) -> dict:
        with open(self._snapshot_path(run_id)) as f:
            return yaml.safe_load(f) or {}

    def _peek_state(self, run_id: str) -> dict:
        return dict(yg.peek(self._app(run_id), run_id).state or {})

    def _is_subcall_parked(self, record: RunRecord) -> bool:
        return record.status == "awaiting" and (record.brief or {}).get("step") == "_subcall"

    def _maybe_park_subcall(self, run_id: str, step: str, vstatus: str, adv: yg.Advance) -> dict | None:
        """Park persistence: when a resume routed into the subcall triple and
        parked on the `_subcall` marker, persist the FULL marker (plus
        `started_at`) as the index brief and tell the worker a subcall
        started — never hand it `step: "_subcall", task: ""` as work."""
        if adv.done or adv.brief is None or adv.brief.step != "_subcall":
            return None
        marker = dict(adv.brief.raw)                   # FULL marker: node/runner/prompt/...
        marker["started_at"] = time.time()
        self._runs.update(run_id, step="_subcall", brief=marker)
        self._log_transition(run_id, step, vstatus, "_subcall")
        return {"accepted": True, "passed": vstatus == "pass", "step": "_subcall",
                "done": False, "subcall": {"node": marker.get("node"),
                                           "runner": marker.get("runner"),
                                           "running_minutes": 0}}

    def _predict_spawn(self, run_id: str, vstatus: str) -> str | None:
        """Static pre-resume route prediction over the SNAPSHOT (never the
        live recipe). Sound because the profile pins spawn-edge conditions
        to exactly verdict_status == '(pass|fail)' and replicates v1's
        loop-guard preemption: a pass AT the cap routes to escalate, not
        the spawn (guard is pre-execution >=, count starts 0)."""
        doc = self._snapshot_doc(run_id)
        nodes = doc.get("nodes") or {}
        tools = doc.get("tools") or {}
        record = self._runs.get(run_id)
        interrupt = next((n for n, cfg in nodes.items()
                          if isinstance(cfg, dict) and cfg.get("type") == "interrupt"
                          and (cfg.get("message") or {}).get("step") == record.step), None)
        if interrupt is None:
            return None
        edges = doc.get("edges") or []
        validator_targets = {e.get("to") for e in edges if e.get("from") == interrupt}
        if len(validator_targets) != 1:
            return None
        validator = next(iter(validator_targets))
        limit = (doc.get("loop_limits") or {}).get(validator)
        if limit is not None:
            counts = self._peek_state(run_id).get("_loop_counts") or {}
            if counts.get(validator, 0) >= limit:
                return None                                # loop guard preempts the spawn route
        for e in edges:
            if (e.get("from") == validator
                    and (e.get("condition") or "").strip() == f"verdict_status == '{vstatus}'"):
                target = e.get("to")
                if profile_check.subcall_node_kind(nodes.get(target), tools) == "spawn":
                    return target
        return None

    def _spawn_marker(self, doc: dict, spawn_node: str) -> dict:
        nodes = doc.get("nodes") or {}
        for e in doc.get("edges") or []:
            if e.get("from") == spawn_node:
                return dict((nodes.get(e.get("to")) or {}).get("message") or {})
        raise LockstepError(f"spawn node '{spawn_node}' has no outgoing marker edge in the snapshot")

    def _check_subcall_policy(self, run_id: str, record: RunRecord, spawn_node: str) -> str | None:
        doc = self._snapshot_doc(run_id)
        marker = self._spawn_marker(doc, spawn_node)
        try:
            spec = runners.resolve(self._state_dir, marker.get("runner"), os.environ)
        except runners.RunnerError as exc:
            return str(exc)
        siblings_dir = self._state_dir / "runs" / f"{run_id}.subcalls"
        node_id = str(marker.get("node"))
        used = ([p for p in siblings_dir.glob("*") if p.is_dir() and p.name != node_id]
                if siblings_dir.exists() else [])
        # one workdir == one node == at most one session ever (the
        # single-start claim): counting sibling dirs IS counting sessions.
        # Re-entry into the same spawn node cannot re-spawn — spawn
        # reattaches to the recorded session (and the profile forbids
        # authored retry-into-spawn edges anyway); do NOT "fix" the
        # reattach by deleting the claim.
        if len(used) >= spec.max_subcalls_per_run:
            return (f"subcall budget exhausted: {len(used)} used, "
                    f"max_subcalls_per_run={spec.max_subcalls_per_run}")
        if marker.get("scenario"):
            # visited-set guard, symmetric with RunIndex.descendants —
            # parent_run is immutable and API-created records cannot cycle,
            # but the walk should not trust that unconditionally.
            depth, cur, seen = 0, record, {record.run_id}
            while cur.parent_run is not None and cur.parent_run not in seen:
                seen.add(cur.parent_run)
                depth += 1
                cur = self._runs.get(cur.parent_run)
            if depth + 1 > spec.max_fractal_depth:
                return (f"fractal depth limit: child would be at depth {depth + 1}, "
                        f"max_fractal_depth={spec.max_fractal_depth}")
        return None

    # ------------------------------------------------------------------
    # Task 7: fractal child runs — creation + recursive termination
    # ------------------------------------------------------------------

    def _start_child(self, parent: RunRecord, scenario: str) -> tuple[str, str]:
        """Mint the child run through the ONE launch path — from the child
        recipe copy PINNED at the parent's start (C2), never the live
        recipes dir: the spawn happens one or more worker turns after
        start(), and the live file is agent-writable in that window. The
        pinned copy carries the parent run's provenance; the child's own
        markers (depth-2) are pinned again at the child's start."""
        nonce = secrets.token_hex(16)
        pinned = self._child_snapshot_path(parent.run_id, scenario)
        if not pinned.exists():
            raise LockstepError(
                f"pinned child recipe missing for run {parent.run_id}: {scenario!r} "
                "(the parent snapshot was made without it — state dir corrupt?)")
        out = self._launch(scenario, {}, parent.project,
                           parent_run=parent.run_id, nonce=nonce, src=pinned)
        return out["run_id"], nonce

    def _ensure_child(self, parent: RunRecord, scenario: str, workdir: Path) -> tuple[str, str]:
        """C7.1 idempotence, race-safe (fresh finding A): a plain
        read-check-create on child.json is last-writer-wins under two
        concurrent scenario_done calls parked on the same marker — both
        would mint a child, and the runner spawned with the LOSING child's
        nonce could never drive the winner's child. So the workdir's
        child.json is CLAIMED with O_CREAT|O_EXCL BEFORE _start_child (the
        claim pattern of locking.file_lock / the single-start claim); the
        loser reads back the winner's (child_run, index-record nonce).
        Called ONLY from _subcall_ctx (spawn path) — _poll_ctx reads
        child.json and NEVER creates."""
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        child_file = workdir / "child.json"
        deadline = time.time() + 30.0
        while True:
            try:
                fd = os.open(child_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    data = json.loads(child_file.read_text())
                except (OSError, ValueError):
                    data = None
                if isinstance(data, dict) and data.get("child_run"):
                    child_run = str(data["child_run"])
                    return child_run, self._runs.get(child_run).nonce
                # Claimed but unfilled: the winner is mid-_start_child (wait
                # for the fill) or died between claim and fill. O_EXCL can
                # never win against an EXISTING empty file, so self-healing
                # needs the stale claim removed first — only after a wait
                # long past any legitimate fill.
                if time.time() > deadline:
                    try:
                        os.unlink(child_file)
                    except OSError:
                        pass
                    deadline = time.time() + 30.0
                else:
                    time.sleep(0.01)
                continue
            os.close(fd)
            try:
                child_run, nonce = self._start_child(parent, scenario)
            except BaseException:
                try:
                    os.unlink(child_file)   # release the claim: nothing was minted
                except OSError:
                    pass
                raise
            tmp = workdir / f"child.json.{os.getpid()}.{time.time_ns()}.tmp"
            tmp.write_text(json.dumps({"child_run": child_run}))
            os.replace(tmp, child_file)     # atomic fill: a reader never sees a torn record
            return child_run, nonce

    def _cascade_terminate(self, run_id: str) -> None:
        """I7.6/I7.7: called after EVERY index write that sets a terminal
        status (double-calls are harmless: terminate()'s verdict claim is
        first-writer-wins and the terminal CAS is idempotent). Recursion is
        RunIndex.descendants — grandchildren included, cycle-safe."""
        affected = [self._runs.get(run_id)] + self._runs.descendants(run_id)
        # kill FIRST, then flip: a flipped-but-alive child could still race
        # one last tool call (its server refuses on the terminal record
        # either way, but the order removes the window instead of
        # tolerating it).
        for rec in affected:
            subcalls_dir = self._state_dir / "runs" / f"{rec.run_id}.subcalls"
            if subcalls_dir.exists():
                for wd in subcalls_dir.glob("*"):
                    if wd.is_dir():
                        subcalls.terminate(wd)   # claims 'cancelled' first-writer-wins; already-terminal workdirs untouched
        for rec in self._runs.descendants(run_id):
            if rec.status not in TERMINAL_STATUSES:
                self._runs.update(rec.run_id, status="aborted")   # CAS skips completed children
        self._nudge_ancestors(run_id)

    def _nudge_ancestors(self, run_id: str) -> None:
        """Liveness nudge: the server is not a daemon — a run parked on a
        subcall advances only when some call touches THAT run, and a
        terminal transition is the last instant this run's session is
        provably alive. So, as the transition's closing act, poll each
        ancestor once via self.status() — the engine equivalent of
        scenario_status: read-only, not origin-bound — letting a parent
        parked on this subcall observe the terminal child even when its own
        worker session is dead. Sits at the end of _cascade_terminate
        because that is the one call every terminal-setting index write
        already makes, and no sidecar lock is held here (RunIndex acquires
        and releases per call; locking.file_lock is non-reentrant).
        Visited-set walk, same shape as RunIndex.descendants — a forged
        on-disk parent_run cycle must not loop. Best-effort by contract:
        this run's terminal outcome is already durable, so no failure here
        may ever surface to the caller."""
        try:
            seen = {run_id}
            parent_id = self._runs.get(run_id).parent_run
            while parent_id is not None and parent_id not in seen:
                seen.add(parent_id)
                try:
                    self.status(parent_id)
                except Exception:  # noqa: BLE001 - one dead ancestor must not stop the walk
                    pass
                parent_id = self._runs.get(parent_id).parent_run
        except Exception:  # noqa: BLE001 - the nudge never affects the child's own result
            pass

    def _subcall_ctx(self, run_id: str, record: RunRecord, spawn_node: str) -> dict:
        doc = self._snapshot_doc(run_id)
        marker = self._spawn_marker(doc, spawn_node)
        spec = runners.resolve(self._state_dir, marker.get("runner"), os.environ)
        # owner budget is a CEILING: the recipe may tighten, never exceed.
        # `.get(key, default)` only substitutes the default when the key is
        # ABSENT — a marker carrying `timeout_minutes: null` still has the
        # key, so `.get` returns None and `int(None)` would raise uncaught.
        # Coerce explicitly: null reads the same as absent.
        tm = marker.get("timeout_minutes")
        timeout = min(int(tm) if tm is not None else spec.timeout_minutes,
                      spec.timeout_minutes)
        node_id = str(marker["node"])
        workdir = self._state_dir / "runs" / f"{run_id}.subcalls" / node_id
        prompt = marker["prompt"]                          # profile-required; KeyError impossible on a profiled snapshot
        child_run = nonce = None
        if marker.get("scenario"):
            child_run, nonce = self._ensure_child(record, str(marker["scenario"]), workdir)
            # C3: the child learns its run id HERE, in an engine-generated
            # preamble prepended to the author prompt — never author-supplied
            # (the author prompt is verbatim recipe text; the run id exists
            # only now). Without this the spawned session must guess its run
            # from the SessionStart listing or shell out for the env var.
            prompt = (
                f"Your lockstep child run id is {child_run}. The engine started that run "
                "for you and this session holds its credential — no other session can "
                "report for it. Drive THAT run with the lockstep MCP tools "
                "mcp__lockstep__scenario_status and mcp__lockstep__scenario_done (exact "
                "tool names — if your harness defers MCP tools behind a tool-search "
                "mechanism, load them by these names first; never substitute shell "
                f"commands for them): call scenario_status with run_id {child_run!r} to "
                "see the parked step, do the work, then report it via scenario_done on "
                "the same run id (mcp__lockstep__scenario_escalate if blocked).\n\n"
                + prompt
            )
        env = runners.child_env(os.environ, self._state_dir, child_run, nonce)
        env["LOCKSTEP_RECIPES"] = str(self._recipes_dir)   # child resolves recipes where its parent did — pinned, not inherited
        argv = subcalls.safe_argv(spec, prompt, None, None)  # model defaults to spec.models[0]; v2 never resumes runner sessions
        return {
            "_subcall_workdir": str(workdir), "_subcall_argv": argv,
            "_subcall_cwd": record.project, "_subcall_env": env,
            "_subcall_timeout_minutes": timeout,
            "_subcall_node": node_id, "_subcall_runner": spec.name,
            "_subcall_child_run": child_run,
            "_subcall_state_dir": str(self._state_dir),
            "_subcall_artifacts": dict(marker.get("artifacts") or {}),
        }

    def _poll_ctx(self, run_id: str, record: RunRecord) -> dict:
        """Slim tick: never resends argv/env/cwd — the spawn already
        happened, and rebroadcasting the credential into the evidence
        channel on every tick would expose it to every later _peek_state.
        Built from the PERSISTED raw marker."""
        marker = record.brief or {}
        node_id = marker.get("node")
        if not node_id:
            raise LockstepError(f"run {run_id}: parked subcall brief lacks 'node' — index corrupt")
        workdir = self._state_dir / "runs" / f"{run_id}.subcalls" / str(node_id)
        child_run = None
        child_file = workdir / "child.json"
        if child_file.exists():
            child_run = json.loads(child_file.read_text()).get("child_run")
        return {
            "_subcall_poll": True,
            "_subcall_workdir": str(workdir),
            "_subcall_node": node_id, "_subcall_runner": marker.get("runner"),
            "_subcall_child_run": child_run,
            "_subcall_state_dir": str(self._state_dir),
            "_subcall_artifacts": dict(marker.get("artifacts") or {}),
        }

    def _auto_poll(self, run_id: str) -> RunRecord:
        """ONE poll resume per entry — if the subcall is still running,
        return immediately; never wait, never loop. Replicates done()'s
        post-resume bookkeeping for every outcome. NO baseline advance on
        any path here: a subcall is not a checked step."""
        record = self._runs.get(run_id)
        payload = self._poll_ctx(run_id, record)
        adv = yg.resume(self._app(run_id), payload, run_id)
        if adv.done:
            # v1's done tail records the final step name, not a placeholder —
            # "_subcall" is the marker's own message.step, never a real
            # recipe step; the parked brief's `node` names the subcall that
            # actually just finished.
            marker = record.brief or {}
            self._runs.update(run_id, status="done", step=marker.get("node", "_subcall"), brief=None)
            self._cascade_terminate(run_id)
            self._log_transition(run_id, "_subcall", "done", None)
        elif adv.brief is not None and adv.brief.step == "escalate":
            env = self._peek_state(run_id).get("_subcall_envelope") or {}
            reason = "; ".join(str(r) for r in (env.get("reasons") or [])) or "subcall failed"
            self._runs.update(run_id, status="escalated", step="escalate",
                              brief={"step": "escalate", "reason": reason})
            self._cascade_terminate(run_id)
            self._log_transition(run_id, "_subcall", "error", "escalate")
        elif adv.brief is not None and adv.brief.step == "_subcall":
            pass                                           # still parked; index brief already true
        elif adv.brief is not None:
            vars_ = self._read_vars(run_id)
            substituted = self._substitute_brief(adv.brief, vars_)
            self._runs.update(run_id, step=substituted.step,
                              brief=self._brief_to_dict(substituted))
            self._log_transition(run_id, "_subcall", "done", substituted.step)
        return self._runs.get(run_id)
