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


def write_runners_yaml(
    state_dir: Path,
    mode: str = "ok",
    sleep: float = 0.0,
    max_subcalls: int = 2,
    max_depth: int = 2,
    runner: str = "claude",
    driver: str = "claude",
    model: str = "claude-haiku-4-5",
    extra_runners: list[tuple[str, str, str]] | None = None,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)          # BEFORE any write
    launcher = state_dir / "runner.py"
    launcher.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import runpy, sys
        sys.argv = [sys.argv[0], "--mode", "{mode}", "--sleep", "{sleep}"] + sys.argv[1:]
        runpy.run_path({str(FAKE)!r}, run_name="__main__")
    """))                                                  # backslash-newline: shebang IS line 1
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    configured = [(runner, driver, model), *(extra_runners or [])]
    entries = "\n".join(
        textwrap.dedent(f"""\
          {name}:
            driver: {entry_driver}
            path: {launcher}
            models: [{entry_model}]
            timeout_minutes: 5""")
        for name, entry_driver, entry_model in configured
    )
    (state_dir / "runners.yaml").write_text(
        "runners:\n"
        f"{textwrap.indent(entries, '  ')}\n"
        f"budgets: {{max_subcalls_per_run: {max_subcalls}, "
        f"max_fractal_depth: {max_depth}}}\n"
    )


def make_engine(tmp_path, monkeypatch, **kw):
    runner = kw.pop("runner", "claude")
    driver = kw.pop("driver", "claude")
    model = kw.pop("model", "claude-haiku-4-5")
    extra_runners = kw.pop("extra_runners", None)
    monkeypatch.setenv("LOCKSTEP_RUNNER", runner)
    state = tmp_path / "state"
    write_runners_yaml(
        state,
        runner=runner,
        driver=driver,
        model=model,
        extra_runners=extra_runners,
        **kw,
    )
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Engine(state_dir=state, recipes_dir=FIX / "good", memory_only=False), proj


def pass_plan(engine, proj: Path, run: dict) -> dict:
    plan = proj / ".lockstep" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("x")
    return engine.done(run["run_id"], "plan", {"plan_path": ".lockstep/plan.md"})
