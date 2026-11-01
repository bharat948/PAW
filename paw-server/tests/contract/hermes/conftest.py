"""S0 contract suite wiring.

Default: runs against the in-process fake (documented surface only), which validates the harness and the
documented-protocol tests. Set PAW_HERMES_URL and PAW_HERMES_KEY to run the SAME tests against a real pinned
Hermes; tests marked hermes_real (model behaviour) then also run. Results against the real image are the S0 evidence.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fake_hermes import KEY, FakeHermes
from .hermes_client import HermesClient

REAL = bool(os.environ.get("PAW_HERMES_URL"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if REAL:
        return
    skip = pytest.mark.skip(reason="needs a real Hermes: set PAW_HERMES_URL and PAW_HERMES_KEY")
    for item in items:
        if "hermes_real" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def hermes() -> Iterator[HermesClient]:
    if REAL:
        yield HermesClient(os.environ["PAW_HERMES_URL"], os.environ.get("PAW_HERMES_KEY"))
        return
    fake = FakeHermes()
    yield HermesClient(fake.url, KEY)
    fake.close()


@pytest.fixture()
def capture(request: pytest.FixtureRequest):
    """Q3: persist raw observations when PAW_HERMES_CAPTURE_DIR is set (real runs)."""
    out = os.environ.get("PAW_HERMES_CAPTURE_DIR")

    def write(name: str, obj: object) -> None:
        if out and REAL:
            d = Path(out)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{request.node.name}-{name}.json").write_text(
                json.dumps(obj, indent=1, default=str)
            )

    return write
