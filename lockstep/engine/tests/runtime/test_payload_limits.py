from __future__ import annotations

import math

import pytest

from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json


@pytest.mark.parametrize(
    "value",
    [
        [[[[[[[[[[[[[[[[["too deep"]]]]]]]]]]]]]]]]],
        [None] * 4097,
        "x" * (64 * 1024 + 1),
        2**63,
        math.nan,
        b"not JSON",
        "\ud800",
        {"\ud800": "bad key"},
    ],
)
def test_json_boundary_rejects_each_hard_limit(value):
    with pytest.raises(PayloadLimitExceeded):
        bounded_json(value, label="test payload")


def test_json_boundary_rejects_total_canonical_size_and_detaches_result():
    oversized = ["x" * 60_000 for _ in range(18)]
    with pytest.raises(PayloadLimitExceeded, match="byte limit"):
        bounded_json(oversized, label="test payload")

    source = {"nested": [1, "ok"]}
    admitted = bounded_json(source, label="test payload")
    source["nested"].append("changed")
    assert admitted == {"nested": [1, "ok"]}
