"""Runner registry: owner-controlled allowlist, absolute paths, budgets.

OS-AGNOSTIC: executability is discovered at runtime (``os.access(path,
os.X_OK)``) — never inferred from platform or location. The engine NEVER
PATH-resolves a runner: on no-sudo hosts a PATH entry is agent-writable,
so a planted binary would forge an "independent" session. That is the
boundary this module holds — it does NOT make the allowlisted path itself
tamper-proof (see ``verified_path``), and it cannot separate the engine
from an agent running as the same OS user (see ``assert_state_dir_sane``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULTS = {"timeout_minutes": 30, "max_subcalls_per_run": 8, "max_fractal_depth": 2}
_BUDGET_KEYS = tuple(DEFAULTS)
_RUNNER_KEYS = {"path", "models", *_BUDGET_KEYS}
# Process essentials only (POSIX + Windows). No SHELL — a `-p` child spawns
# no interactive shell. LOCKSTEP_RECIPES passes through so a fractal child
# resolves recipes where its parent did, not against its own cwd.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SystemRoot", "COMSPEC", "PATHEXT", "USERPROFILE",
    "LOCKSTEP_RECIPES",
)


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


def _as_int(name: str, key: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise RunnerError(f"runner '{name}': {key} must be an integer, got {value!r}") from e


def load_runners(state_dir: Path) -> dict[str, RunnerSpec]:
    cfg_path = Path(state_dir) / "runners.yaml"
    if not cfg_path.exists():
        return {}
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    budgets = cfg.get("budgets") or {}
    out: dict[str, RunnerSpec] = {}
    for name, body in (cfg.get("runners") or {}).items():
        body = body or {}
        unknown = set(body) - _RUNNER_KEYS
        if unknown:
            # A misspelled budget key would otherwise parse clean and
            # silently fall back to defaults.
            raise RunnerError(f"runner '{name}': unknown key(s) {sorted(unknown)}")
        # every budget honours runner-body override -> top-level budgets -> default
        limits = {
            k: _as_int(name, k, body.get(k, budgets.get(k, DEFAULTS[k]))) for k in _BUDGET_KEYS
        }
        out[name] = RunnerSpec(
            name=name,
            path=str(body.get("path", "")),
            models=list(body.get("models") or []),
            **limits,
        )
    return out


def verified_path(spec: RunnerSpec) -> str:
    """Verify the runner binary; call this ADJACENT to the spawn, never
    cached across it. ``resolve()``'s identical check is time-of-check
    only: the allowlisted path itself (or a symlink component) may be
    user-writable — e.g. ``/opt/homebrew/bin/claude`` is user-owned on
    macOS — so the boundary is "the engine never PATH-resolves and never
    execs an unverified path", NOT "only a runners.yaml compromise can
    swap the binary". Re-verifying at spawn narrows the window; it cannot
    close it for a path the same user can write."""
    if not spec.path or not os.path.isabs(spec.path):
        raise RunnerError(f"runner '{spec.name}': path must be absolute, got {spec.path!r}")
    if not (os.path.isfile(spec.path) and os.access(spec.path, os.X_OK)):
        raise RunnerError(f"runner '{spec.name}': {spec.path} is not an executable file")
    return spec.path


def assert_state_dir_sane(state_dir: Path, project: Path) -> None:
    """Refuse a state dir inside the project tree.

    The state dir holds the trust anchor (``runners.yaml``) and the run
    index, and ``LOCKSTEP_STATE_DIR`` arrives from the environment
    unvalidated — while the project tree is exactly the zone the gated
    agent writes. A state dir inside it hands the agent the allowlist.
    RESIDUAL (documented, not closable here): in the target deployment
    the agent runs as the SAME OS user as the engine, so a state dir
    anywhere that user can write remains reachable outside the gate;
    this check closes only the in-project (under-the-gate) placement.
    """
    state = Path(state_dir).resolve()
    proj = Path(project).resolve()
    if state == proj or proj in state.parents:
        raise RunnerError(
            f"state dir {state} lies inside the project tree {proj} — "
            "the runner allowlist and run index would be agent-writable"
        )


def resolve(state_dir: Path, node_runner: str | None, env: Mapping[str, str]) -> RunnerSpec:
    registry = load_runners(state_dir)
    name = node_runner or env.get("LOCKSTEP_RUNNER")
    if not name:
        raise RunnerError("no runner named on the node and no LOCKSTEP_RUNNER default from the adapter")
    if name not in registry:
        raise RunnerError(f"runner '{name}' is not in the owner allowlist ({state_dir}/runners.yaml)")
    spec = registry[name]
    if not spec.models:
        # fail closed: an absent/empty models list would accept ANY model
        raise RunnerError(f"runner '{name}': models allowlist is required and must be non-empty")
    verified_path(spec)
    return spec


def build_argv(spec: RunnerSpec, prompt: str, model: str | None, resume_session: str | None) -> list[str]:
    if not spec.models:
        raise RunnerError(f"runner '{spec.name}': models allowlist is required and must be non-empty")
    if model is None:
        model = spec.models[0]  # never inherit the binary's own default
    if model not in spec.models:
        raise RunnerError(f"runner '{spec.name}': model {model!r} not in allowlist {spec.models}")
    # The prompt is assembled from a brief carrying WORKER-SUPPLIED vars: it
    # goes LAST, behind an explicit `--` terminator, so a prompt starting
    # with `-`/`--` can never parse as a flag. `--model` is ALWAYS emitted.
    argv = [spec.path, "-p", "--output-format", "json", "--model", model]
    if resume_session:
        argv += ["--resume", resume_session]
    argv += ["--", prompt]
    return argv


def child_env(base_env: Mapping[str, str], state_dir: Path,
              child_run: str | None, nonce: str | None) -> dict[str, str]:
    if (child_run is None) != (nonce is None):
        # fail closed: one half of the credential would spawn a child
        # indistinguishable from an intentional one-shot
        raise RunnerError("child_run and nonce must be passed together (or neither)")
    env = {k: base_env[k] for k in _ENV_ALLOWLIST if k in base_env}
    env["LOCKSTEP_STATE_DIR"] = str(state_dir)
    if child_run is not None:
        env["LOCKSTEP_CHILD_RUN"] = child_run
        env["LOCKSTEP_CHILD_NONCE"] = nonce
    return env
