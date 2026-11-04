from decimal import Decimal

import pytest

from paw_server.modules.costs.pricing import CostError, Rate

RATE = Rate(
    "v",
    "gpt-4o-mini",
    Decimal(".15"),
    Decimal(".60"),
    Decimal(".075"),
    128000,
    16384,
    ("gpt-4o-mini",),
)


def test_exact_cached_pricing_and_conservative_bound():
    assert RATE.cost(1000, 100, 500) == Decimal(".000172500")
    assert RATE.reserve(4096) == Decimal(".021657600")
    assert RATE.cost(1, 0) == Decimal(".000000150")


@pytest.mark.parametrize("args", [(-1, 0, 0), (2, 1, 3), (True, 0, 0), (1, None, 0)])
def test_invalid_usage_never_becomes_zero(args):
    with pytest.raises(CostError):
        RATE.cost(*args)
