"""Local embedding generation using sentence-transformers with instant deterministic fallback."""
from __future__ import annotations

import os
import hashlib
import numpy as np
from typing import Optional

_model = None
_model_name = "all-MiniLM-L6-v2"
_use_fallback = False


def _get_model():
    global _model, _use_fallback
    # If in test mode or fallback forced, return None immediately
    if os.environ.get("DATABASE_URL") == "sqlite:///:memory:" or os.environ.get("TESTING") == "true" or _use_fallback:
        return None

    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(_model_name)
        except Exception:
            _use_fallback = True
            return None
    return _model


def _fallback_embedding(text: str, dim: int = 384) -> list[float]:
    """Fast, deterministic fallback vector for offline / test environments."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand hash to 384 floats
    seed = int.from_bytes(h[:4], "big")
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def embed_text(text: str) -> list[float]:
    """Embed a single text string locally."""
    model = _get_model()
    if model is not None:
        try:
            embedding = model.encode(text, normalize_embeddings=True)
            return embedding.tolist()
        except Exception:
            pass
    return _fallback_embedding(text)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts."""
    if not texts:
        return []
    model = _get_model()
    if model is not None:
        try:
            embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
            return [e.tolist() for e in embeddings]
        except Exception:
            pass
    return [_fallback_embedding(t) for t in texts]


def embedding_dimension() -> int:
    model = _get_model()
    if model is not None:
        try:
            return model.get_sentence_embedding_dimension()
        except Exception:
            pass
    return 384
