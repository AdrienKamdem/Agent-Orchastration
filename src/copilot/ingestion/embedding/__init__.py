"""Embedding models behind one small interface.

* ``SentenceTransformerEmbedder``: the real thing. Runs locally on CPU, free.
  ``BAAI/bge-small-en-v1.5`` gives 384-dim vectors, 512-token max input. bge models want a
  short instruction prefix on *queries* (not documents) for retrieval, applied here.
* ``HashingEmbedder``: bag-of-words feature hashing, no dependencies, deterministic. It
  only captures word overlap (no semantics), so use it for tests and smoke runs, not
  for real retrieval.

Vectors are L2-normalised, so cosine similarity is a dot product.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

SCHEMA_DIM = 384  # must match chunks.embedding vector(384) in the migration


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class HashingEmbedder:
    def __init__(self, dim: int = SCHEMA_DIM):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"\w+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim))

    def embed_query(self, text: str) -> np.ndarray:
        return self._one(text)


class SentenceTransformerEmbedder:
    BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str, batch_size: int = 32):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "sentence-transformers is not installed. Run `uv sync --extra embeddings`, "
                "or set EMBEDDING_BACKEND=hashing for a dependency-free smoke test."
            ) from e
        self._model = SentenceTransformer(model_name)
        self.name = model_name
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)
        self.batch_size = batch_size
        self._query_prefix = self.BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True)
        )

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(
            self._model.encode([self._query_prefix + text], normalize_embeddings=True)[0]
        )


def make_embedder(backend: str, model_name: str, batch_size: int = 32) -> Embedder:
    emb: Embedder
    if backend == "hashing":
        emb = HashingEmbedder()
    elif backend == "sentence-transformers":
        emb = SentenceTransformerEmbedder(model_name, batch_size)
    else:
        raise ValueError(f"unknown embedding backend {backend!r}")
    if emb.dim != SCHEMA_DIM:
        raise ValueError(
            f"{emb.name} produces {emb.dim}-dim vectors but the schema stores {SCHEMA_DIM}. "
            "Pick a 384-dim model or add a migration (see docs/ingestion/PLAN.md section 5)."
        )
    return emb
