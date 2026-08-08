"""Shared helpers for the v2 subcall engine/server/integration tests.

State dir and project are SIBLINGS under tmp_path — Engine.start() refuses
a state dir inside the project tree (runners.assert_state_dir_sane).
"""
import stat
import textwrap
from pathlib import Path

from lockstep_mcp.engine import Engine

FIX = Path(__file__).parent / "fixtures" / "recipes"
FAKE = Path(__file__).parent / "fixtures" / "fake_runner.py"


def write_runners_yaml(state_dir: Path, mode: str = "ok", sleep: float = 0.0,
                       max_subcalls: int = 2, max_depth: int = 2) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)          # BEFORE any write
    launcher = state_dir / "runner.py"
    launcher.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import runpy, sys
        sys.argv = [sys.argv[0], "--mode", "{mode}", "--sleep", "{sleep}"] + sys.argv[1:]
        runpy.run_path({str(FAKE)!r}, run_name="__main__")
    """))                                                  # backslash-newline: shebang IS line 1
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    (state_dir / "runners.yaml").write_text(textwrap.dedent(f"""\
        runners:
          claude:
            path: {launcher}
            models: [claude-haiku-4-5]
            timeout_minutes: 5
        budgets: {{max_subcalls_per_run: {max_subcalls}, max_fractal_depth: {max_depth}}}
    """))                                                  # non-empty models: resolve() fails closed on []


def make_engine(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("LOCKSTEP_RUNNER", "claude")
    state = tmp_path / "state"
    write_runners_yaml(state, **kw)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Engine(state_dir=state, recipes_dir=FIX / "good", memory_only=False), proj


def pass_plan(engine, proj: Path, run: dict) -> dict:
    plan = proj / ".lockstep" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("x")
    return engine.done(run["run_id"], "plan", {"plan_path": ".lockstep/plan.md"})
