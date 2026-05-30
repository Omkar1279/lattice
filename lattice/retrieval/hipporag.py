from typing import List, Any
from lattice.core.interfaces import Chunk
from lattice.retrieval.bm25 import BM25SearchStrategy
import numpy as np

def hipporag_retrieve(db: Any, query: str, limit: int) -> List[Chunk]:
    # Step 1: seed chunks via BM25
    seeds = BM25SearchStrategy(db).search(query, 3)
    if not seeds:
        return []
    
    seed_ids = {s.id for s in seeds}
    
    # Step 2: build adjacency from edges table
    outgoing = {}
    nodes = set(seed_ids)
    for r in db.execute("SELECT source_chunk_id, target_chunk_id FROM edges WHERE kind IN ('imports', 'calls')"):
        src, tgt = r[0], r[1]
        nodes.add(src)
        nodes.add(tgt)
        outgoing.setdefault(src, []).append(tgt)
        
    node_arr = list(nodes)
    idx = {n: i for i, n in enumerate(node_arr)}
    n = len(node_arr)
    
    # Step 3: personalized PageRank via power iteration
    alpha = 0.85
    teleport = np.zeros(n, dtype=np.float64)
    for id_ in seed_ids:
        teleport[idx[id_]] = 1.0 / len(seed_ids)
            
    rank = teleport.copy()
    for _ in range(10):
        next_rank = np.zeros(n, dtype=np.float64)
        for i in range(n):
            neighbors = outgoing.get(node_arr[i])
            if not neighbors:
                continue
            share = rank[i] / len(neighbors)
            for nb in neighbors:
                if nb in idx:
                    next_rank[idx[nb]] += share
        rank = alpha * next_rank + (1.0 - alpha) * teleport
        
    # Step 4: sort by rank, fetch chunks
    top_ids = [node_arr[i] for i in np.argsort(-rank)[:limit]]
    
    chunks = []
    for cid in top_ids:
        row = db.execute("SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL", (cid,)).fetchone()
        if row:
            chunks.append(Chunk.from_dict(dict(row)))
    return chunks
