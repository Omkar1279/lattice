import struct
import os
import numpy as np
from typing import List, Any
from lattice.core.interfaces import Chunk

EMBEDDING_DIM = 384
_projection: List[List[float]] = None
_embedder = None

def get_projection() -> List[List[float]]:
    global _projection
    if _projection is not None:
        return _projection
    
    seed = 42
    projection = []
    for _ in range(EMBEDDING_DIM):
        row = []
        for _ in range(EMBEDDING_DIM):
            seed = (seed * 1664525 + 1013904223) & 0xffffffff
            val = seed / 0xffffffff
            row.append(-1.0 if val < 0.5 else 1.0)
        projection.append(row)
    _projection = projection
    return _projection

def get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder
    from fastembed import TextEmbedding
    # bge-small-en-v1.5 is 384 dimensions
    _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedder

def embed_colbert(text: str) -> List[float]:
    model = get_embedder()
    # Extract sequence-level token embeddings (batch_size, seq_len, 384)
    # onnx_embed returns OnnxOutputContext
    out = model.model.onnx_embed([text])
    # out.model_output has shape (1, seq_len, 384)
    tokens = out.model_output[0]  # shape: (seq_len, 384)
    
    proj = get_projection()
    result = np.full(EMBEDDING_DIM, -np.inf, dtype=np.float32)
    proj_arr = np.array(proj, dtype=np.float32) # shape: (384, 384)
    
    for tok in tokens:
        dots = np.dot(proj_arr, tok) # shape: (384,)
        np.maximum(result, dots, out=result)
        
    return result.tolist()

def embed_and_store_colbert(db: Any, chunk_id: str, text: str) -> None:
    if os.environ.get("LATTICE_COLBERT") != "on":
        return
    vec = embed_colbert(text)
    vec_bytes = struct.pack(f"{EMBEDDING_DIM}f", *vec)
    db.execute("DELETE FROM chunks_colbert WHERE chunk_id = ?", (chunk_id,))
    db.execute("INSERT INTO chunks_colbert (chunk_id, embedding) VALUES (?, ?)", (chunk_id, vec_bytes))

def search_colbert(db: Any, query: str, limit: int) -> List[Chunk]:
    query_vec = embed_colbert(query)
    vec_bytes = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)
    
    sql = """
        SELECT c.*
        FROM chunks_colbert v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE v.embedding MATCH ?
          AND k = ?
          AND c.superseded_by IS NULL
        ORDER BY distance
        LIMIT ?
    """
    rows = [dict(r) for r in db.execute(sql, (vec_bytes, limit, limit)).fetchall()]
    return [Chunk.from_dict(r) for r in rows]
