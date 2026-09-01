from pathlib import Path

from lockstep.runtime.hooks import hook_session_start, hook_stop


def test_hooks_read_native_projection_without_mutating_catalog(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    before = tuple(state.rglob("*"))
    assert hook_stop({}, state, str(tmp_path)) == (0, "")
    assert hook_session_start(state, str(tmp_path)) == ""
    assert tuple(state.rglob("*")) == before
