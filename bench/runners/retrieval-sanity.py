#!/usr/bin/env python3
"""
Python port of bench/runners/retrieval-sanity.ts
Retrieval sanity check.
Indexes fastapi fixture and reports R@5 / R@10 / MRR on queries.yaml.
Writes to bench/results/retrieval-sanity.json.
"""

import os
import sys
import json
import time
import yaml
import tempfile
import shutil
import subprocess
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from lattice.storage.vault import open_vault
from lattice.indexer.indexer import reindex_repo
from lattice.retrieval.cascade import CascadePipelineFactory

FIXTURE_REPO_PATH = "/tmp/lattice-bench-fastapi-0.115.0"
FIXTURE_SHA = "40e33e492dbf4af6172997f4e3238a32e56cbe26"

def verify_fixture():
    if not os.path.exists(FIXTURE_REPO_PATH):
        raise RuntimeError(
            f"Fixture not found at {FIXTURE_REPO_PATH}. "
            f"Re-clone with: git clone --depth 1 --branch 0.115.0 https://github.com/tiangolo/fastapi {FIXTURE_REPO_PATH}"
        )
    # Check HEAD SHA
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=FIXTURE_REPO_PATH).decode().strip()
    except Exception as e:
        raise RuntimeError(f"Failed to check git SHA: {e}")
        
    if head != FIXTURE_SHA:
        raise RuntimeError(f"Fixture SHA mismatch. expected={FIXTURE_SHA} got={head}. Re-clone cleanly.")

def main():
    started_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    print("→ verifying fixture...", file=sys.stderr)
    verify_fixture()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
    queries_path = os.path.join(repo_root, "bench/fixtures/queries.yaml")
    print(f"→ loading {queries_path}...", file=sys.stderr)
    with open(queries_path, "r", encoding="utf-8") as f:
        queries_doc = yaml.safe_load(f)
        
    queries = queries_doc.get("queries", [])
    print(f"  {len(queries)} queries loaded", file=sys.stderr)

    vault_dir = tempfile.mkdtemp(prefix="lattice-sanity-")
    print(f"→ opening fresh vault at {vault_dir}...", file=sys.stderr)
    vault = open_vault(vault_dir)

    try:
        print(f"→ indexing {FIXTURE_REPO_PATH}...", file=sys.stderr)
        t0 = time.time()
        reindex_repo(vault, FIXTURE_REPO_PATH)
        elapsed = time.time() - t0
        
        # Get statistics
        chunk_count = vault.db.execute("SELECT COUNT(*) as n FROM chunks WHERE source = 'code_index'").fetchone()["n"]
        symbol_count = vault.db.execute("SELECT COUNT(*) as n FROM symbols").fetchone()["n"]
        print(f"  indexed {chunk_count} chunks, {symbol_count} symbols in {elapsed:.1f}s\n", file=sys.stderr)

        SOURCE_SCOPE = os.path.join(FIXTURE_REPO_PATH, "fastapi") + "/"
        pipeline = CascadePipelineFactory.create(vault.db)
        
        results = []
        for q in queries:
            cascade_results = pipeline.retrieve(
                query=q["query"],
                budget_tokens=100000,
                kind="all",
                path_scope=SOURCE_SCOPE
            )
            
            ranked = []
            for i, r in enumerate(cascade_results):
                chunk_id = r.chunk.id
                # Get the chunk path
                row = vault.db.execute("SELECT path FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                if row and row["path"]:
                    rel_path = os.path.relpath(row["path"], FIXTURE_REPO_PATH)
                else:
                    rel_path = None
                ranked.append({
                    "rank": i + 1,
                    "path": rel_path,
                    "heading": r.chunk.heading
                })
                
            required_paths = [req["path"] for req in q["required"]]
            acceptable_paths = [acc["path"] for acc in q.get("acceptable", [])]
            valid_paths = set(required_paths + acceptable_paths)
            
            hit_rank = -1
            for idx, item in enumerate(ranked):
                if item["path"] and item["path"] in valid_paths:
                    hit_rank = idx
                    break
                    
            first_hit_rank = hit_rank + 1 if hit_rank >= 0 else None
            
            results.append({
                "id": q["id"],
                "category": q["category"],
                "query": q["query"],
                "required_paths": required_paths,
                "acceptable_paths": acceptable_paths,
                "hit_at_5": first_hit_rank is not None and first_hit_rank <= 5,
                "hit_at_10": first_hit_rank is not None and first_hit_rank <= 10,
                "first_hit_rank": first_hit_rank,
                "top_5": ranked[:5]
            })
            
            status = "✗ MISS" if first_hit_rank is None else "✓" if first_hit_rank <= 5 else "△"
            print(f"  {q['id']} [{q['category'].ljust(11)}] rank={first_hit_rank or '-'}  {status}  \"{q['query'][:60]}\"", file=sys.stderr)
            
        # Aggregate stats
        overall = {
            "n": len(results),
            "r_at_5": len([r for r in results if r["hit_at_5"]]) / len(results) if results else 0,
            "r_at_10": len([r for r in results if r["hit_at_10"]]) / len(results) if results else 0,
            "mrr": sum(1.0 / r["first_hit_rank"] for r in results if r["first_hit_rank"] is not None) / len(results) if results else 0
        }
        
        by_category = {}
        for cat in ["symbol", "behavioural"]:
            subset = [r for r in results if r["category"] == cat]
            if not subset:
                continue
            by_category[cat] = {
                "n": len(subset),
                "r_at_5": len([r for r in subset if r["hit_at_5"]]) / len(subset),
                "r_at_10": len([r for r in subset if r["hit_at_10"]]) / len(subset),
                "mrr": sum(1.0 / r["first_hit_rank"] for r in subset if r["first_hit_rank"] is not None) / len(subset)
            }
            
        symbol_r5 = by_category.get("symbol", {}).get("r_at_5", 0)
        verdict = f"PASS: symbol R@5 = {symbol_r5:.2f} >= 0.9." if symbol_r5 >= 0.9 else f"FAIL: symbol R@5 = {symbol_r5:.2f} < 0.9."
        
        output = {
            "meta": {
                "date": started_at,
                "fixture": queries_doc.get("fixture", "fastapi@0.115.0"),
                "fixture_path": FIXTURE_REPO_PATH,
                "vault_path": vault_dir,
                "chunks_indexed": chunk_count,
                "symbols_indexed": symbol_count,
                "index_elapsed_sec": elapsed,
                "python": sys.version,
                "platform": sys.platform,
                "methodology": "single condition (full cascade), path-level R@K."
            },
            "overall": overall,
            "by_category": by_category,
            "verdict": verdict,
            "per_query": results
        }
        
        out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../results'))
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "retrieval-sanity.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
            f.write("\n")
            
        print(f"\n→ wrote {os.path.relpath(out_path, repo_root)}", file=sys.stderr)
        print(f"\noverall (n={overall['n']}):", file=sys.stderr)
        print(f"  R@5  = {overall['r_at_5']:.2f}  R@10 = {overall['r_at_10']:.2f}  MRR  = {overall['mrr']:.2f}", file=sys.stderr)
        for cat, stats in by_category.items():
            print(f"{cat.ljust(11)} (n={stats['n']}):  R@5  = {stats['r_at_5']:.2f}  R@10 = {stats['r_at_10']:.2f}  MRR  = {stats['mrr']:.2f}", file=sys.stderr)
        print(f"\n{verdict}", file=sys.stderr)
        
    finally:
        vault.close()
        shutil.rmtree(vault_dir, ignore_errors=True)

if __name__ == '__main__':
    main()
