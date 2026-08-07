"""Runner registry: owner-controlled allowlist, absolute paths, budgets.

OS-AGNOSTIC: executability is discovered at runtime (``os.access(path,
os.X_OK)``) — never inferred from platform or location. The engine NEVER
PATH-resolves a runner: on no-sudo hosts a PATH entry is agent-writable,
so a planted binary would forge an "independent" session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

DEFAULTS = {"timeout_minutes": 30, "max_subcalls_per_run": 8, "max_fractal_depth": 2}
_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SHELL")


class RunnerError(RuntimeError):
    """Runner missing from the allowlist, misconfigured, or unusable."""


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    path: str
    models: list[str]
    timeout_minutes: int
    max_subcalls_per_run: int
    max_fractal_depth: int


def load_runners(state_dir: Path) -> dict[str, RunnerSpec]:
    cfg_path = Path(state_dir) / "runners.yaml"
    if not cfg_path.exists():
        return {}
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    budgets = cfg.get("budgets") or {}
    out: dict[str, RunnerSpec] = {}
    for name, body in (cfg.get("runners") or {}).items():
        body = body or {}
        out[name] = RunnerSpec(
            name=name,
            path=str(body.get("path", "")),
            models=list(body.get("models") or []),
            timeout_minutes=int(body.get("timeout_minutes", budgets.get("timeout_minutes", DEFAULTS["timeout_minutes"]))),
            max_subcalls_per_run=int(budgets.get("max_subcalls_per_run", DEFAULTS["max_subcalls_per_run"])),
            max_fractal_depth=int(budgets.get("max_fractal_depth", DEFAULTS["max_fractal_depth"])),
        )
    return out


def resolve(state_dir: Path, node_runner: str | None, env: Mapping[str, str]) -> RunnerSpec:
    registry = load_runners(state_dir)
    name = node_runner or env.get("LOCKSTEP_RUNNER")
    if not name:
        raise RunnerError("no runner named on the node and no LOCKSTEP_RUNNER default from the adapter")
    if name not in registry:
        raise RunnerError(f"runner '{name}' is not in the owner allowlist ({state_dir}/runners.yaml)")
    spec = registry[name]
    if not spec.path or not os.path.isabs(spec.path):
        raise RunnerError(f"runner '{name}': path must be absolute, got {spec.path!r}")
    if not (os.path.isfile(spec.path) and os.access(spec.path, os.X_OK)):
        raise RunnerError(f"runner '{name}': {spec.path} is not an executable file")
    return spec


def build_argv(spec: RunnerSpec, prompt: str, model: str | None, resume_session: str | None) -> list[str]:
    if model and spec.models and model not in spec.models:
        raise RunnerError(f"runner '{spec.name}': model {model!r} not in allowlist {spec.models}")
    argv = [spec.path, "-p", prompt, "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if resume_session:
        argv += ["--resume", resume_session]
    return argv


def child_env(spec: RunnerSpec, base_env: Mapping[str, str], state_dir: Path,
              child_run: str | None, nonce: str | None) -> dict[str, str]:
    env = {k: base_env[k] for k in _ENV_ALLOWLIST if k in base_env}
    env["LOCKSTEP_STATE_DIR"] = str(state_dir)
    if child_run and nonce:
        env["LOCKSTEP_CHILD_RUN"] = child_run
        env["LOCKSTEP_CHILD_NONCE"] = nonce
    return env
