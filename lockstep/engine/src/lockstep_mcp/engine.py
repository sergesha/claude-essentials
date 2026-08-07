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
  disagreement — EXCEPT it never overrides a terminal (`aborted`/
  `escalated`) index status; those have no checkpoint counterpart to
  disagree with in the first place.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from lockstep_mcp import evidence as evidence_mod
from lockstep_mcp import profile_check
from lockstep_mcp import validators
from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.runs import RunIndex, RunRecord

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


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
        if record.status in ("escalated", "aborted"):
            # terminal index statuses have no checkpoint counterpart to
            # disagree with — never overridden.
            return record

        try:
            adv = yg.peek(self._app(run_id), run_id)
        except Exception:  # noqa: BLE001 - reconcile is best-effort; index truth stands
            return record

        if adv.done:
            if record.status != "done":
                record = self._runs.update(run_id, status="done", brief=None)
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
        vars = vars or {}
        src = self.recipe_path(recipe)
        if not src.exists():
            raise LockstepError(f"recipe not found: {recipe}")
        raw_bytes = src.read_bytes()

        runs_dir = self._runs_dir()
        staging = runs_dir / f".staging-{uuid.uuid4().hex}.yaml"
        staging.write_bytes(raw_bytes)
        try:
            profile_errors = profile_check.check_recipe(staging)
            if profile_errors:
                raise LockstepError(
                    f"recipe {recipe!r} failed profile check: " + "; ".join(profile_errors)
                )
            ok, msg = yg.cli_validate(staging)
            if not ok:
                raise LockstepError(f"recipe {recipe!r} failed to compile: {msg}")

            record = self._runs.create(recipe, project)
            staging.replace(self._snapshot_path(record.run_id))
        finally:
            if staging.exists():
                staging.unlink()

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

    def status(self, run_id: str) -> dict:
        record = self._reconcile(run_id)
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
        }

    def done(self, run_id: str, step: str, evidence: dict) -> dict:
        record = self._reconcile(run_id)
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

        resume_payload = {**raw_evidence, "_verdict_status": vstatus, "_verdict_reasons": reasons}
        adv = yg.resume(self._app(run_id), resume_payload, run_id)

        if vstatus == "fail":
            if not adv.done and adv.brief is not None and adv.brief.step == "escalate":
                self._runs.update(
                    run_id,
                    status="escalated",
                    step="escalate",
                    brief={"step": "escalate", "reason": "; ".join(reasons) or "loop limit reached"},
                )
                return {
                    "accepted": True,
                    "passed": False,
                    "reasons": reasons,
                    "step": step,
                    "escalated": True,
                }

            vars_ = self._read_vars(run_id)
            substituted = self._substitute_brief(adv.brief, vars_)
            self._runs.update(
                run_id, step=substituted.step, brief=self._brief_to_dict(substituted)
            )
            return {"accepted": True, "passed": False, "reasons": reasons, "step": step}

        # vstatus == "pass"
        self._advance_baseline(run_id, record.project, globs)

        if adv.done:
            self._runs.update(run_id, status="done", step=step, brief=None)
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
        return {
            "accepted": True,
            "passed": True,
            "step": substituted.step,
            "done": False,
            "task": substituted.task,
            "exit_criterion": substituted.exit_criterion,
        }

    def escalate(self, run_id: str, reason: str) -> dict:
        record = self._reconcile(run_id)
        if record.status != "awaiting":
            raise LockstepError(f"run {run_id} is {record.status} — terminal")
        self._runs.update(
            run_id,
            status="escalated",
            step="escalate",
            brief={"step": "escalate", "reason": reason},
        )
        return {"run_id": run_id, "status": "escalated", "reason": reason}

    def abort(self, run_id: str) -> dict:
        record = self._reconcile(run_id)
        if record.status != "awaiting":
            raise LockstepError(f"run {run_id} is {record.status} — terminal")
        self._runs.update(run_id, status="aborted")
        return {"run_id": run_id, "status": "aborted"}
