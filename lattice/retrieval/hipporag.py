from typing import List, Any
from lattice.core.interfaces import Chunk
from lattice.retrieval.bm25 import BM25SearchStrategy
import numpy as np

def hipporag_retrieve(db: Any, query: str, limit: int) -> List[Chunk]:
    # Step 1: seed chunks via BM25
    bm25 = BM25SearchStrategy(db)
    seeds = bm25.search(query, 3)
    if not seeds:
        return []
    
    seed_ids = {s.id for s in seeds}
    
    # Step 2: build weighted adjacency from edges table
    WEIGHTS = {
        'calls': 1.0,
        'imports': 0.8,
        'inherits': 0.9,
        'references': 0.5,
        'defines': 1.0
    }
    
    rows = db.execute("SELECT source_chunk_id AS source, target_chunk_id AS target, kind FROM edges WHERE confidence >= 0.5").fetchall()
    
    outgoing = {}
    nodes = set()
    for r in rows:
        src = r["source"]
        tgt = r["target"]
        kind = r["kind"]
        w = WEIGHTS.get(kind, 1.0)
        
        nodes.add(src)
        nodes.add(tgt)
        outgoing.setdefault(src, []).append((tgt, w))
        
    for id_ in seed_ids:
        nodes.add(id_)
        
    node_arr = list(nodes)
    idx = {n: i for i, n in enumerate(node_arr)}
    n = len(node_arr)
    if n == 0:
        return seeds[:limit]
        
    # Step 3: personalized PageRank via power iteration
    alpha = 0.85
    teleport = np.zeros(n, dtype=np.float64)
    for id_ in seed_ids:
        if id_ in idx:
            teleport[idx[id_]] = 1.0 / len(seed_ids)
            
    rank = teleport.copy()
    for _ in range(10):
        next_rank = np.zeros(n, dtype=np.float64)
        for i in range(n):
            neighbors = outgoing.get(node_arr[i])
            if not neighbors:
                continue
            total_w = sum(w for _, w in neighbors)
            if total_w == 0:
                continue
            for nb, w in neighbors:
                if nb in idx:
                    next_rank[idx[nb]] += rank[i] * (w / total_w)
        rank = alpha * next_rank + (1.0 - alpha) * teleport
        
    # Step 4: sort by rank, fetch chunks
    scored = [{"id": node_arr[i], "score": rank[i]} for i in range(n)]
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_scored = scored[:limit]
    
    chunks = []
    for s in top_scored:
        row = db.execute("SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL", (s["id"],)).fetchone()
        if row:
            chunks.append(Chunk.from_dict(dict(row)))
    return chunks
