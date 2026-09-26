"""Ordinary YAMLGraph Python tool; no wrapper calls or monkeypatches."""

from langgraph.config import get_config


def observe(_state):
    return {"observed": get_config()["configurable"]["sentinel"]}
