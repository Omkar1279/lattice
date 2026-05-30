"""Semantic search — Strategy Pattern with Config-Driven factory.

Uses a generic SingletonRegistry to eliminate duplicated factory boilerplate.
"""

import struct
import threading
from typing import Any, List, Optional

from lattice.core.config import get_config
from lattice.core.interfaces import Chunk, Embedder, Reranker, SearchStrategy


# ---------------------------------------------------------------------------
# Generic Singleton Registry (eliminates duplicated Factory classes)
# ---------------------------------------------------------------------------

class SingletonRegistry:
    """Thread-safe registry for lazily-initialized singletons."""

    _instances: dict = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, key: str, factory_fn):
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    cls._instances[key] = factory_fn()
        return cls._instances[key]

    @classmethod
    def reset(cls) -> None:
        """Reset all singletons (for testing)."""
        with cls._lock:
            cls._instances.clear()


# ---------------------------------------------------------------------------
# Null Object Pattern — used when features are disabled
# ---------------------------------------------------------------------------

class NullEmbedder(Embedder):
    @property
    def dimension(self) -> int:
        return 768

    def embed(self, text: str) -> List[float]:
        return []


class NullReranker(Reranker):
    def rerank(self, query: str, chunks: List[Chunk]) -> List[Chunk]:
        return chunks


# ---------------------------------------------------------------------------
# Concrete implementations
# ---------------------------------------------------------------------------

class FastEmbedEmbedder(Embedder):
    """Embedder using fastembed with lazy model loading."""

    DIMENSION = 768

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding
                    self._model = TextEmbedding(model_name=self._model_name)

    def embed(self, text: str) -> List[float]:
        self._ensure_model()
        return list(next(self._model.embed([text])))

    @property
    def dimension(self) -> int:
        return self.DIMENSION


class CrossEncoderReranker(Reranker):
    """Reranker using fastembed cross-encoder with lazy model loading."""

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextCrossEncoder
                    self._model = TextCrossEncoder(model_name=self._model_name)

    def rerank(self, query: str, chunks: List[Chunk]) -> List[Chunk]:
        if not chunks:
            return chunks

        self._ensure_model()
        documents = [c.body for c in chunks]
        results = list(self._model.rerank(query, documents))

        reranked: List[Chunk] = []
        for res in results:
            idx = getattr(res, 'corpus_id', None)
            if idx is None and isinstance(res, dict):
                idx = res.get('corpus_id')
            if idx is not None and idx < len(chunks):
                reranked.append(chunks[idx])
        return reranked


# ---------------------------------------------------------------------------
# Config-driven factories (single line each, no boilerplate)
# ---------------------------------------------------------------------------

def create_embedder() -> Embedder:
    cfg = get_config()
    if not cfg.embeddings_enabled:
        return NullEmbedder()
    return SingletonRegistry.get_or_create(
        'embedder', lambda: FastEmbedEmbedder(cfg.embed_model)
    )


def create_reranker() -> Reranker:
    cfg = get_config()
    if not cfg.reranker_enabled:
        return NullReranker()
    return SingletonRegistry.get_or_create(
        'reranker', lambda: CrossEncoderReranker(cfg.rerank_model)
    )


# ---------------------------------------------------------------------------
# Search Strategy
# ---------------------------------------------------------------------------

class SemanticSearchStrategy(SearchStrategy):
    """ANN Semantic Search using sqlite-vec."""

    def __init__(self, db: Any, embedder: Optional[Embedder] = None):
        self._db = db
        self._embedder = embedder or create_embedder()

    def search(
        self,
        query: str,
        limit: int,
        path_scope: Optional[str] = None,
        source_filter: Optional[List[str]] = None,
    ) -> List[Chunk]:
        query_vec = self._embedder.embed(query)
        if not query_vec:
            return []

        vec_bytes = struct.pack(f'{self._embedder.dimension}f', *query_vec)
        # Over-fetch when filtering by path (vec0 doesn't support pre-filtering)
        k = limit * 2 if path_scope else limit

        sql = '''
            SELECT c.*
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.chunk_id
            WHERE v.embedding MATCH ? AND k = ?
              AND c.superseded_by IS NULL
        '''
        params: list = [vec_bytes, k]

        if path_scope:
            sql += '  AND c.path LIKE ?\n'
            params.append(f'{path_scope}%')

        sql += '            ORDER BY distance'
        rows = [dict(r) for r in self._db.execute(sql, params).fetchall()]
        return [Chunk.from_dict(r) for r in rows[:limit]]

    def embed_and_store(self, chunk_id: str, text: str) -> None:
        """Embed and store in sqlite-vec table."""
        vector = self._embedder.embed(text)
        if not vector:
            return

        vec_bytes = struct.pack(f'{self._embedder.dimension}f', *vector)
        self._db.execute('DELETE FROM chunks_vec WHERE chunk_id = ?', [chunk_id])
        self._db.execute(
            'INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)',
            [chunk_id, vec_bytes],
        )
