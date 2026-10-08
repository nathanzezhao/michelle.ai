"""Local text embeddings via Ollama.

Ungated helper for pad follow-ups. Retrieve v2 indexing stays behind
RETRIEVE_V2 in retrieve.py. Env is read at call time because load_dotenv
in main.py runs after some modules import.
"""

from __future__ import annotations

import os

import httpx


def embed_text(text: str) -> list[float] | None:
    blob = (text or "").strip()
    if not blob:
        return None
    base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("EMBED_MODEL") or os.getenv(
        "RETRIEVE_EMBED_MODEL", "nomic-embed-text"
    )
    try:
        response = httpx.post(
            f"{base}/api/embeddings",
            json={"model": model, "prompt": blob},
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
        embedding = payload.get("embedding")
        if isinstance(embedding, list) and embedding:
            return [float(x) for x in embedding]
    except Exception as exc:
        print(f"[embeddings] unavailable ({exc})")
    return None


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
