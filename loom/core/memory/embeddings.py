"""
Embedding Provider — vector representations for semantic memory search.

Provides language-agnostic similarity search as the primary tier of the
multi-fallback recall chain: embedding > BM25 > recency.

Supports two backends controlled by loom.toml [embeddings] provider:
  - "ollama"    → local Ollama server (no API key required)
  - "minimax"   → MiniMax embedding API (API key required)

Usage
-----
    from loom.core.memory.embeddings import build_embedding_provider
    provider = build_embedding_provider(env, cfg)
    if provider:
        vectors = await provider.embed(["Loom is a harness-first framework"])
        # → [[0.023, -0.14, ...]]

loom.toml example:
    [embeddings]
    provider = "ollama"           # "ollama" or "minimax"
    base_url = "http://localhost:11434"   # Ollama server
    model    = "qwen3-embedding:0.6b"    # Ollama embedding model

    # For MiniMax (requires API key):
    # provider     = "minimax"
    # api_key_env  = "MINIMAX_API_KEY"
    # model       = "embo-01"
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Return one embedding vector per input text.

        Parameters
        ----------
        texts:  List of strings to embed (batch for efficiency).

        Returns
        -------
        A list of float lists, one per input text, in the same order.
        """
        ...


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaEmbeddingProvider(EmbeddingProvider):
    """
    Local Ollama server embedding API via ``POST /api/embed``.

    Model ``qwen3-embedding:0.6b`` produces 512-dimensional vectors.

    The Ollama ``/api/embed`` endpoint accepts:
      - ``input``: a single string  OR
      - ``input``: a list of strings (batch)
      - ``model``: model name

    Response:
      ``{"model": "...", "embeddings": [[float, ...], ...], ...}``
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-embedding:0.6b",
    ) -> None:
        import httpx
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.post(
            f"{self._base_url}/api/embed",
            json={
                "model": self._model,
                "input": texts,          # Ollama handles list batching natively
            },
        )
        response.raise_for_status()
        body = response.json()
        embeddings = body.get("embeddings")
        if embeddings is None:
            raise RuntimeError(
                f"Ollama embedding API returned no embeddings "
                f"(response={body})"
            )
        return embeddings

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# MiniMax
# ---------------------------------------------------------------------------

class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """
    MiniMax embedding API via direct HTTP calls.

    Model ``embo-01`` produces 1536-dimensional vectors.

    Note: The MiniMax embedding endpoint is NOT OpenAI-compatible — it uses
    ``texts`` (not ``input``) in the request and returns ``vectors`` (not
    ``data[i].embedding``) in the response.  We use httpx directly instead
    of the OpenAI SDK to avoid the "No embedding data received" ValueError.
    """

    EMBEDDING_MODEL = "embo-01"
    BASE_URL = "https://api.minimax.io/v1"

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        import httpx
        self._api_key = api_key
        self._base_url = (base_url or self.BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.post(
            f"{self._base_url}/embeddings",
            json={
                "model": self.EMBEDDING_MODEL,
                "texts": texts,
                "type": "query",
            },
        )
        response.raise_for_status()
        body = response.json()

        base = body.get("base_resp", {})
        status_code = base.get("status_code", 0)
        if status_code and status_code != 0:
            raise RuntimeError(
                f"MiniMax embedding API error {status_code}: "
                f"{base.get('status_msg', 'unknown error')}"
            )

        vectors = body.get("vectors")
        if not vectors:
            raise RuntimeError(
                f"MiniMax embedding API returned no vectors "
                f"(base_resp={base})"
            )
        # Response: {"vectors": [[float, ...], [float, ...]], ...}
        return [v if isinstance(v, list) else v["embedding"] for v in vectors]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity between two vectors (pure Python, no numpy).
    Returns a value in [-1, 1]; higher means more similar.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_embedding_provider(
    env: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> EmbeddingProvider | None:
    """
    Construct an embedding provider from loom.toml [embeddings] configuration.

    Provider selection (loom.toml [embeddings] provider field):
        "ollama"   → OllamaEmbeddingProvider (no API key needed)
                     Requires: base_url, model
        "minimax"  → MiniMaxEmbeddingProvider (API key required)
                     Requires: api_key_env (or MINIMAX_API_KEY env var)

    Returns None when:
        - ``provider`` is not set in loom.toml
        - "minimax" is selected but no API key is found
        - the configured provider is unknown

    Callers must handle the None case and fall through to BM25 search.

    loom.toml example:

        [embeddings]
        provider  = "ollama"
        base_url  = "http://localhost:11434"
        model     = "qwen3-embedding:0.6b"

        # For MiniMax instead:
        # provider    = "minimax"
        # api_key_env = "EMBEDDING_API_KEY"
        # model       = "embo-01"
        # base_url    = "https://api.minimax.io/v1"
    """
    cfg = cfg or {}
    embeddings_cfg = cfg.get("embeddings", {})
    provider_name: str = embeddings_cfg.get("provider", "").lower()

    if not provider_name:
        return None

    if provider_name == "ollama":
        base_url = embeddings_cfg.get("base_url", "http://localhost:11434")
        model = embeddings_cfg.get("model", "qwen3-embedding:0.6b")
        return OllamaEmbeddingProvider(base_url=base_url, model=model)

    if provider_name == "minimax":
        import os
        key_env_name = embeddings_cfg.get("api_key_env", "")
        if key_env_name:
            key = env.get(key_env_name) or os.environ.get(key_env_name, "")
        else:
            key = env.get("minimax.io_key") or env.get("MINIMAX_API_KEY") or ""

        if not key:
            logger.warning(
                "[embeddings] provider='minimax' but no API key found; "
                "embedding disabled."
            )
            return None

        base_url = embeddings_cfg.get("base_url") or MiniMaxEmbeddingProvider.BASE_URL
        return MiniMaxEmbeddingProvider(api_key=key, base_url=base_url)

    logger.warning(
        "[embeddings] unknown provider %r — embedding disabled. "
        "Valid values: 'ollama', 'minimax'.",
        provider_name,
    )
    return None
