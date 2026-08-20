def capture(state, config):
    configurable = config["configurable"]
    return {
        "seen": state.get("seed", "captured"),
        "observed_thread_id": str(configurable.get("thread_id", "")),
        "observed_checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
        "observed_checkpoint_id": str(configurable.get("checkpoint_id", "")),
    }


def persist_identity(state):
    return dict(state["answer"])
