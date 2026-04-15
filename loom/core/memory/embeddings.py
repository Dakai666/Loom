"""
Embedding Provider — vector representations for semantic memory search.

Provides language-agnostic similarity search as the primary tier of the
multi-fallback recall chain: embedding > BM25 > recency.

Usage
-----
    provider = MiniMaxEmbeddingProvider(api_key="...")
    vectors = await provider.embed(["Loom is a harness-first framework"])
    # → [[0.023, -0.14, ...]]   (1536-dim float list per text)

The MiniMax embedding endpoint uses a non-OpenAI format (``texts``/``vectors``
instead of ``input``/``data``), so we use httpx directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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


class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """
    MiniMax embedding API via direct HTTP calls.

    Model ``embo-01`` produces 1536-dimensional vectors.

    Note: The MiniMax embedding endpoint is NOT OpenAI-compatible — it uses
    ``texts`` (not ``input``) in the request and returns ``vectors`` (not
    ``data[i].embedding``) in the response.  We use httpx directly instead
    of the OpenAI SDK to avoid the "No embedding data received" ValueError.

    Each call to ``embed()`` issues one API request.  For batch writes
    (e.g. compressing 7 facts at session end) pass all texts in a single
    call rather than looping.
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


def build_embedding_provider(
    env: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> MiniMaxEmbeddingProvider | None:
    """
    Construct a MiniMaxEmbeddingProvider from the loaded .env dict.
    Returns None if no API key is found — callers must handle the None case
    and fall through to BM25 search.

    Configuration priority:
    1. loom.toml [embeddings] api_key_env — name of the env var holding the key
       (allows a dedicated embedding key separate from the chat API key)
    2. MINIMAX_API_KEY / minimax.io_key  — shared fallback (default)

    Example loom.toml:
        [embeddings]
        api_key_env = "EMBEDDING_API_KEY"   # optional dedicated key
    """
    cfg = cfg or {}
    embeddings_cfg = cfg.get("embeddings", {})
    key_env_name: str = embeddings_cfg.get("api_key_env", "")

    if key_env_name:
        import os
        key = env.get(key_env_name) or os.environ.get(key_env_name, "")
    else:
        key = (
            env.get("minimax.io_key")
            or env.get("MINIMAX_API_KEY")
            or ""
        )

    if not key:
        return None

    # Allow overriding the base URL via [embeddings] base_url in loom.toml.
    # MINIMAX_API_HOST / minimax.api_host are chat-endpoint aliases that lack
    # the /v1 suffix, so we do NOT use them here — the class constant is correct.
    base_url: str = embeddings_cfg.get("base_url") or MiniMaxEmbeddingProvider.BASE_URL
    return MiniMaxEmbeddingProvider(api_key=key, base_url=base_url)
