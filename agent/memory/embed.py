"""Embeddings (Voyage voyage-3.5, 1024 dims) and a token estimator. Plain httpx, no extra dependency."""
import os
import time
from typing import Optional, Protocol

import httpx
import tiktoken

MODEL, DIM = "voyage-3.5", 1024
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """ESTIMATE (cl100k). DeepSeek's own tokenizer is not available offline; real counts come from LLM usage."""
    return len(_enc.encode(text))


def truncate_tokens(text: str, max_tokens: int) -> str:
    ids = _enc.encode(text)
    return text if len(ids) <= max_tokens else _enc.decode(ids[:max_tokens])


class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]: ...


class VoyageEmbedder:
    def __init__(self, api_key: str):
        self.key = api_key

    def __init__(self, api_key: str, retries: int = 3, max_wait: float = 25.0):
        self.key, self.retries, self.max_wait = api_key, retries, max_wait

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """Retries on 429 (low-tier Voyage keys allow only a few requests per minute), honouring Retry-After."""
        for attempt in range(self.retries + 1):
            r = httpx.post("https://api.voyageai.com/v1/embeddings", headers={"Authorization": f"Bearer {self.key}"},
                           json={"input": texts, "model": MODEL, "input_type": input_type, "output_dimension": DIM},
                           timeout=30)
            if r.status_code == 429 and attempt < self.retries:
                time.sleep(min(float(r.headers.get("retry-after") or 21), self.max_wait))
                continue
            r.raise_for_status()
            return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def get_embedder() -> Optional[Embedder]:
    key = os.environ.get("VOYAGE_API_KEY")
    return VoyageEmbedder(key) if key else None
