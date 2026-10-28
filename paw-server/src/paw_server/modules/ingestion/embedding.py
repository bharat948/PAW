"""Embedding provider adapter (D004). Business code depends only on `Embedder`."""

import hashlib
import math
import re
from typing import Protocol

import httpx

from paw_server.core.config import Settings
from paw_server.modules.ingestion.errors import PermanentIngestError, TransientIngestError


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class FakeEmbedder:
    """Deterministic hashed bag-of-words (no network, no cost). Shares vocabulary => similar vectors."""

    def __init__(self, model: str = "fake-hash-v1", dim: int = 1536) -> None:
        self.model, self.dim = model, dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"\w+", t.lower()):
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
                v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class OpenAICompatEmbedder:
    """OpenAI-compatible /embeddings endpoint with timeout and retry classification."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model, self.dim = model, dim
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self._client.post(
                "/embeddings", json={"model": self.model, "input": texts, "dimensions": self.dim}
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientIngestError("embedding provider unreachable") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientIngestError(f"embedding provider returned {r.status_code}")
        if r.status_code >= 400:
            raise PermanentIngestError(
                "embedding_rejected", f"provider rejected request ({r.status_code})"
            )
        try:
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
        except (KeyError, ValueError, TypeError) as exc:
            raise TransientIngestError("malformed embedding response") from exc
        if len(vectors) != len(texts):
            raise TransientIngestError("partial embedding batch")  # never accept partial results
        if any(len(v) != self.dim for v in vectors):
            raise PermanentIngestError(
                "embedding_dim_mismatch", "provider returned wrong dimensions"
            )
        return vectors


def build_embedder(s: Settings) -> Embedder:
    if s.embedding_provider == "openai":
        return OpenAICompatEmbedder(
            s.embedding_api_base,
            s.embedding_api_key,
            s.embedding_model,
            s.embedding_dim,
            s.embedding_timeout_s,
        )
    return FakeEmbedder(s.embedding_model, s.embedding_dim)
