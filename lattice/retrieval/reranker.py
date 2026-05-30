import os
from typing import List
from lattice.core.interfaces import Chunk
from lattice.retrieval.semantic import create_reranker

def rerank(query: str, chunks: List[Chunk], top_k: int) -> List[Chunk]:
    if os.environ.get("LATTICE_RERANKER") != "on" or not chunks:
        return chunks[:top_k]
    
    reranker = create_reranker()
    reranked = reranker.rerank(query, chunks)
    return reranked[:top_k]
