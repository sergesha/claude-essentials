from __future__ import annotations


def test_new_owner_descendants_fsync_each_containing_directory(
    tmp_path, monkeypatch
) -> None:
    import lockstep.runtime.owner_state as owner_state

    root = owner_state.initialize_owner_state(tmp_path / "owner")
    observed = []
    original = owner_state.fsync_owner_directory

    def record_and_sync(path):
        observed.append(path)
        original(path)

    monkeypatch.setattr(owner_state, "fsync_owner_directory", record_and_sync)
    created = owner_state.ensure_owner_directory(root, "artifacts/manifests")

    assert created == root / "artifacts/manifests"
    assert observed == [root, root / "artifacts"]
