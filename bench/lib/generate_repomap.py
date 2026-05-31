#!/usr/bin/env python3
"""Generate an Aider-style repository map using standard PageRank over the AST call graph.

Ranks all files and symbols, then formats them into a tree structure.
"""

import os
import sys
import sqlite3
import numpy as np

def generate_map(vault_dir: str, limit: int = 40) -> str:
    db_path = os.path.join(vault_dir, "index.db")
    if not os.path.exists(db_path):
        return "[aider-repomap] index.db not found. Run indexing first."

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # Step 1: build adjacency from edges table
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

    # Also add all chunks in the DB to nodes
    chunk_rows = db.execute("SELECT id, path, heading FROM chunks WHERE superseded_by IS NULL").fetchall()
    for c in chunk_rows:
        nodes.add(c["id"])

    node_arr = list(nodes)
    idx = {n: i for i, n in enumerate(node_arr)}
    n = len(node_arr)
    if n == 0:
        db.close()
        return "[aider-repomap] No indexed symbols."

    # Step 2: standard PageRank via power iteration
    alpha = 0.85
    teleport = np.ones(n, dtype=np.float64) / n
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

    # Step 3: group ranked chunks by path
    chunks_by_id = {c["id"]: c for c in chunk_rows}
    scored = []
    for i in range(n):
        c_id = node_arr[i]
        if c_id in chunks_by_id:
            c = chunks_by_id[c_id]
            scored.append({
                "path": c["path"],
                "heading": c["heading"],
                "score": rank[i]
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_scored = scored[:limit]

    # Group by file path
    by_file = {}
    for s in top_scored:
        by_file.setdefault(s["path"], []).append(s["heading"])

    # Step 4: format into Aider-style text tree
    lines = []
    lines.append("==== AIDER REPO-MAP ====")
    lines.append("Below is a structural map of the most important symbols in this repository:")
    for path, headings in sorted(by_file.items()):
        lines.append(f"{path}:")
        for h in sorted(set(headings)):
            # Indent symbols for tree layout
            lines.append(f"  {h}")
    lines.append("========================")
    
    db.close()
    return "\n".join(lines)

def main():
    if len(sys.argv) < 2:
        print("Usage: generate_repomap.py <vault_dir> [limit]")
        sys.exit(1)
    vault_dir = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    print(generate_map(vault_dir, limit))

if __name__ == "__main__":
    main()
