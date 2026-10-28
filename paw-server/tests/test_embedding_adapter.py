import httpx
import pytest

from paw_server.modules.ingestion.embedding import FakeEmbedder, OpenAICompatEmbedder
from paw_server.modules.ingestion.errors import PermanentIngestError, TransientIngestError


def adapter(handler, dim=4):
    return OpenAICompatEmbedder(
        "http://x/v1", "k", "m", dim, 1.0, transport=httpx.MockTransport(handler)
    )


def ok(dim, n):
    return httpx.Response(
        200, json={"data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)]}
    )


def test_success_and_ordering():
    def h(req):
        return httpx.Response(
            200,
            json={
                "data": [{"index": 1, "embedding": [2.0] * 4}, {"index": 0, "embedding": [1.0] * 4}]
            },
        )

    assert adapter(h).embed(["a", "b"]) == [[1.0] * 4, [2.0] * 4]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_statuses(status):
    with pytest.raises(TransientIngestError):
        adapter(lambda r: httpx.Response(status)).embed(["a"])


def test_timeout_is_transient():
    def h(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientIngestError):
        adapter(h).embed(["a"])


def test_bad_request_is_permanent():
    with pytest.raises(PermanentIngestError):
        adapter(lambda r: httpx.Response(400)).embed(["a"])


def test_wrong_dimension_is_permanent():
    with pytest.raises(PermanentIngestError) as e:
        adapter(lambda r: ok(3, 1)).embed(["a"])
    assert e.value.code == "embedding_dim_mismatch"


def test_partial_batch_is_rejected():
    with pytest.raises(TransientIngestError):
        adapter(lambda r: ok(4, 1)).embed(["a", "b"])


def test_fake_embedder_deterministic_and_related():
    e = FakeEmbedder(dim=64)
    a, b, c = e.embed(["red apple pie", "red apple tart", "quantum chromodynamics"])
    assert e.embed(["red apple pie"])[0] == a
    dot = lambda x, y: sum(p * q for p, q in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)
