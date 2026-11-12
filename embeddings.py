"""Local text embeddings via Sentence Transformers (MiniLM).

Ungated helper for pad follow-ups, the command catalog, and app-name
slots. Retrieve v2 indexing stays behind RETRIEVE_V2 in retrieve.py.
Env is read at call time because load_dotenv in main.py runs after
some modules import. Ollama is not used for embeddings.
"""

from __future__ import annotations

import os
from threading import Lock

DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Leftover Ollama embed ids from .env must not hit /api/embeddings.
_OLLAMA_EMBED_ALIASES = frozenset(
    {
        "nomic-embed-text",
        "nomic-embed-text-v1",
        "nomic-embed-text-v1.5",
        "nomic-embed-text:latest",
        "nomic-embed-text-v1.5:latest",
    }
)

_model = None
_model_id: str | None = None
_load_failed = False
_lock = Lock()


def resolve_model_id() -> str:
    raw = (
        os.getenv("EMBED_MODEL") or os.getenv("RETRIEVE_EMBED_MODEL") or ""
    ).strip()
    if not raw or raw.lower() in _OLLAMA_EMBED_ALIASES:
        return DEFAULT_EMBED_MODEL
    return raw


def _load_model():
    global _model, _model_id, _load_failed
    with _lock:
        if _model is not None:
            return _model
        if _load_failed:
            return None
        model_id = resolve_model_id()
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(model_id, device="cpu")
            _model_id = model_id
            print(f"[embeddings] loaded {model_id} on cpu")
            return _model
        except Exception as exc:
            _load_failed = True
            print(f"[embeddings] unavailable ({exc})")
            return None


def warmup() -> bool:
    """Load weights at process start so the first /chat is not a HF download."""
    global _load_failed
    _load_failed = False
    return _load_model() is not None


def _embed_minilm(text: str) -> list[float] | None:
    blob = (text or "").strip()
    if not blob:
        return None
    model = _load_model()
    if model is None:
        return None
    try:
        vector = model.encode(
            blob,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        if vector is None or getattr(vector, "size", 0) == 0:
            return None
        return [float(x) for x in vector.tolist()]
    except Exception as exc:
        print(f"[embeddings] unavailable ({exc})")
        return None


def embed_text(text: str) -> list[float] | None:
    return _embed_minilm(text)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return round(dot / (na * nb), 3)
