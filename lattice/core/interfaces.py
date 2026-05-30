"""Abstract interfaces for the retrieval module.

ISP: each interface has a single focused responsibility.
DIP: high-level modules depend on these abstractions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Chunk:
    """Value object representing a knowledge chunk."""

    id: str
    heading: str
    body: str
    source: str
    path: str
    tags: List[str] = field(default_factory=list)
    created_at: str = ''
    last_seen_at: str = ''
    last_validated_at: str = ''
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    expansion_queries: Optional[str] = None
    pinned: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Chunk:
        tags_raw = data.get('tags')
        if tags_raw is None:
            tags = []
        elif isinstance(tags_raw, str):
            tags = tags_raw.split(',') if tags_raw else []
        else:
            tags = list(tags_raw)
        return cls(
            id=data['id'],
            heading=data.get('heading', ''),
            body=data.get('body', ''),
            source=data.get('source', 'auto_capture'),
            path=data.get('path', ''),
            tags=tags,
            created_at=data.get('created_at', ''),
            last_seen_at=data.get('last_seen_at', ''),
            last_validated_at=data.get('last_validated_at', ''),
            supersedes=data.get('supersedes'),
            superseded_by=data.get('superseded_by'),
            expansion_queries=data.get('expansion_queries'),
            pinned=bool(data.get('pinned', 0)),
        )


@dataclass
class RetrievalResult:
    """Value object for a retrieval result with scoring metadata."""

    chunk: Chunk
    score: float = 0.0
    freshness: float = 0.0
    preview: str = ''
    tokens: int = 0


class FreshnessScorer(ABC):
    """Strategy interface for computing freshness scores."""

    @abstractmethod
    def score(self, chunk: Chunk, now_ms: float) -> float: ...


class Embedder(ABC):
    """Interface for text embedding."""

    @abstractmethod
    def embed(self, text: str) -> List[float]: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...


class Reranker(ABC):
    """Interface for cross-encoder reranking."""

    @abstractmethod
    def rerank(self, query: str, chunks: List[Chunk]) -> List[Chunk]: ...


class SearchStrategy(ABC):
    """Strategy interface for search implementations."""

    @abstractmethod
    def search(
        self,
        query: str,
        limit: int,
        path_scope: Optional[str] = None,
        source_filter: Optional[List[str]] = None,
    ) -> List[Chunk]: ...


class SymbolResolver(ABC):
    """Interface for exact symbol lookup."""

    @abstractmethod
    def resolve(self, query: str, path_scope: Optional[str] = None) -> List[Chunk]: ...


class ResultFuser(ABC):
    """Interface for fusing multiple ranked lists."""

    @abstractmethod
    def fuse(
        self,
        ranked_lists: List[List[Chunk]],
        since_ts: float,
        source_filter: Optional[List[str]],
        now_ts: float,
    ) -> List[Chunk]: ...


class RetrievalPipeline(ABC):
    """Template Method interface for the full retrieval cascade."""

    @abstractmethod
    def retrieve(self, query: str, **kwargs: Any) -> List[RetrievalResult]: ...
