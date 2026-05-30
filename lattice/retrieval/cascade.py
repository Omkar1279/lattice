"""Retrieval cascade — Template Method + Strategy patterns.

Config-driven pipeline: all tuning knobs come from LatticeConfig.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from lattice.core.config import get_config
from lattice.core.interfaces import (
    Chunk,
    FreshnessScorer,
    Reranker,
    RetrievalPipeline,
    RetrievalResult,
    ResultFuser,
    SearchStrategy,
    SymbolResolver,
)
from lattice.retrieval.bm25 import BM25SearchStrategy
from lattice.retrieval.freshness import ExponentialFreshnessScorer, get_timestamp
from lattice.retrieval.semantic import SemanticSearchStrategy, create_reranker
from lattice.retrieval.symbol import GraphExpandedSymbolResolver
from lattice.util.tokens import count_tokens, truncate_to_budget


# ---------------------------------------------------------------------------
# Config-driven constants (source filter is just a lookup table)
# ---------------------------------------------------------------------------

SOURCE_FILTERS: Dict[str, Optional[List[str]]] = {
    'code': ['code_index'],
    'notes': ['human_note', 'auto_capture'],
    'auto': None,
}

# Compiled regex patterns for identifier detection
_IDENT_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')
_IDENT_SIGNALS = [
    re.compile(r'[._]'),
    re.compile(r'[a-z][A-Z]'),
    re.compile(r'^[A-Z][a-z]'),
    re.compile(r'[A-Z]{2,}[a-z]'),
]
_SPLIT_PATTERN = re.compile(r'[\s,;:?!()[\]{}"\']+')


def is_identifier(q: str) -> bool:
    """Heuristic: does this string look like a code identifier?"""
    if not _IDENT_PATTERN.match(q):
        return False
    return any(p.search(q) for p in _IDENT_SIGNALS)


def extract_identifiers(query: str) -> List[str]:
    """Extract potential code identifiers from a query string."""
    trimmed = query.strip()
    if not trimmed:
        return []
    if len(trimmed) < 80 and is_identifier(trimmed):
        return [trimmed]

    seen: set = set()
    out: List[str] = []
    for tok in _SPLIT_PATTERN.split(trimmed):
        if 3 <= len(tok) < 80 and is_identifier(tok) and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Result Fusion (RRF)
# ---------------------------------------------------------------------------

class ReciprocalRankFuser(ResultFuser):
    """Reciprocal Rank Fusion — merges multiple ranked lists."""

    def __init__(self, k: int | None = None, scorer: Optional[FreshnessScorer] = None):
        self._k = k if k is not None else get_config().rrf_k
        self._scorer = scorer or ExponentialFreshnessScorer()

    def fuse(
        self,
        ranked_lists: List[List[Chunk]],
        since_ts: float,
        source_filter: Optional[List[str]],
        now_ts: float,
    ) -> List[Chunk]:
        score_map: Dict[str, Dict[str, Any]] = {}

        for lst in ranked_lists:
            for rank, chunk in enumerate(lst):
                if chunk.superseded_by:
                    continue
                if get_timestamp(chunk.last_seen_at) < since_ts:
                    continue
                if source_filter and chunk.source not in source_filter:
                    continue

                rrf_score = 1.0 / (self._k + rank + 1)

                if chunk.id in score_map:
                    score_map[chunk.id]['score'] += rrf_score
                else:
                    score_map[chunk.id] = {'chunk': chunk, 'score': rrf_score}

        results = list(score_map.values())
        for res in results:
            res['score'] *= self._scorer.score(res['chunk'], now_ts)

        results.sort(key=lambda x: x['score'], reverse=True)
        return [r['chunk'] for r in results]


# ---------------------------------------------------------------------------
# Budget Packer
# ---------------------------------------------------------------------------

@dataclass
class BudgetPacker:
    """Packs chunks into a token budget, producing RetrievalResults."""

    budget_tokens: int
    scorer: FreshnessScorer

    def pack(self, chunks: List[Chunk], now_ts: float) -> List[RetrievalResult]:
        out: List[RetrievalResult] = []
        used = 0

        for chunk in chunks:
            preview = truncate_to_budget('\n'.join(chunk.body.split('\n')[:2]), 200)
            tokens = count_tokens(preview)

            if used + tokens > self.budget_tokens:
                break

            out.append(RetrievalResult(
                chunk=chunk,
                freshness=self.scorer.score(chunk, now_ts),
                preview=preview,
                tokens=tokens,
            ))
            used += tokens
        return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class CascadeRetrievalPipeline(RetrievalPipeline):
    """Symbol -> BM25/Semantic -> RRF -> Rerank pipeline."""

    def __init__(
        self,
        symbol_resolver: SymbolResolver,
        search_strategies: List[SearchStrategy],
        fuser: ResultFuser,
        reranker: Reranker,
        scorer: FreshnessScorer,
    ):
        self._symbol_resolver = symbol_resolver
        self._search_strategies = search_strategies
        self._fuser = fuser
        self._reranker = reranker
        self._scorer = scorer

    def retrieve(self, query: str, **kwargs: Any) -> List[RetrievalResult]:
        import os
        now_ts = time.time() * 1000
        kind = kwargs.get('kind', 'auto')
        path_scope = kwargs.get('path_scope')
        budget_tokens = kwargs.get('budget_tokens', 2500)
        since = kwargs.get('since')
        since_ts = get_timestamp(since) if since else float('-inf')

        packer = BudgetPacker(budget_tokens=budget_tokens, scorer=self._scorer)

        # Stage 1: Exact symbol match (early exit)
        if kind != 'notes':
            symbol_hits = self._resolve_symbols(query, path_scope)
            if symbol_hits:
                return packer.pack(symbol_hits, now_ts)

        # Stage 2: Parallel BM25 + semantic + ColBERT + HippoRAG
        embeddings_on = os.environ.get("LATTICE_EMBEDDINGS") == "on"
        colbert_on = os.environ.get("LATTICE_COLBERT") == "on"
        hipporag_on = os.environ.get("LATTICE_HIPPORAG") == "on"
        source_filter = SOURCE_FILTERS.get(kind)
        
        db = self._symbol_resolver._db

        # BM25 Search Strategy
        bm25_hits = BM25SearchStrategy(db).search(query, 15, path_scope, source_filter)
        
        # Semantic Search Strategy
        semantic_hits = []
        if embeddings_on:
            semantic_hits = SemanticSearchStrategy(db).search(query, 10, path_scope, source_filter)
            
        # ColBERT Search Strategy
        colbert_hits = []
        if colbert_on:
            from lattice.retrieval.colbert import search_colbert
            colbert_hits = search_colbert(db, query, 10)
            
        # HippoRAG Search Strategy
        hippo_hits = []
        if hipporag_on:
            from lattice.retrieval.hipporag import hipporag_retrieve
            hippo_hits = hipporag_retrieve(db, query, 10)
            
        ranked_lists = [bm25_hits, semantic_hits, colbert_hits, hippo_hits]

        # Stage 3: Fusion
        fused = self._fuser.fuse(ranked_lists, since_ts, source_filter, now_ts)
        
        # Stage 4: Cross-encoder rerank (top 50 -> top 10)
        from lattice.retrieval.reranker import rerank
        reranked = rerank(query, fused[:50], 10)

        # Stage 5: Pack to token budget
        return packer.pack(reranked, now_ts)

    def _resolve_symbols(self, query: str, path_scope: Optional[str]) -> List[Chunk]:
        candidates = extract_identifiers(query)
        trimmed = query.strip()
        word_count = len(trimmed.split())

        for cand in candidates:
            if cand != trimmed and word_count > 5:
                continue
            hits = self._symbol_resolver.resolve(cand, path_scope)
            if hits:
                return hits
        return []


# ---------------------------------------------------------------------------
# Factory (composes the full pipeline from config)
# ---------------------------------------------------------------------------

class CascadePipelineFactory:
    """Creates a fully-configured CascadeRetrievalPipeline from a DB handle."""

    @staticmethod
    def create(db: Any) -> CascadeRetrievalPipeline:
        scorer = ExponentialFreshnessScorer()
        return CascadeRetrievalPipeline(
            symbol_resolver=GraphExpandedSymbolResolver(db),
            search_strategies=[
                BM25SearchStrategy(db),
                SemanticSearchStrategy(db),
            ],
            fuser=ReciprocalRankFuser(scorer=scorer),
            reranker=create_reranker(),
            scorer=scorer,
        )
