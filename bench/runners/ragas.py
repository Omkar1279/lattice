#!/usr/bin/env python3
"""
Python port of bench/runners/ragas.ts
RAGAS triad evaluation — local, LLM-free implementation.
Measures three axes of retrieval quality: Faithfulness, Answer Relevance, and Context Precision.
Plus token cost tracking per query.
Writes to bench/results/ragas.json.
"""

import os
import sys
import json
import re
import yaml
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from lattice.util.tokens import count_tokens

def tokenize(text: str) -> set:
    if not text:
        return set()
    clean = re.sub(r'[^a-z0-9_]', ' ', text.lower())
    return set(w for w in clean.split() if w)

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    intersection = len(a.intersection(b))
    union = len(a.union(b))
    return intersection / union if union > 0 else 0.0

def as_retrieved(c) -> dict:
    if isinstance(c, str):
        return {"id": c, "body": c}
    return c

def evaluate_ragas(queries: list) -> dict:
    scores = []
    for q in queries:
        retrieved = [as_retrieved(c) for c in q["retrieved_chunks"]]
        answer_text = q.get("answer") or ""
        context_text = " ".join(r["body"] for r in retrieved)
        
        query_words = tokenize(q["query"])
        answer_words = tokenize(answer_text)
        context_words = tokenize(context_text)
        
        # Faithfulness: grounded in context
        faithfulness = jaccard(answer_words, context_words) if answer_words else 0.0
        
        # Answer relevance
        answer_relevance = jaccard(query_words, answer_words) if answer_words else 0.0
        
        # Context precision
        expected_set = set(q["expected_chunks"])
        relevant = [c for c in retrieved if c["id"] in expected_set]
        context_precision = len(relevant) / len(retrieved) if retrieved else 0.0
        
        # Token cost
        token_cost = sum(count_tokens(c["body"]) for c in retrieved)
        
        scores.append({
            "query": q["query"],
            "faithfulness": faithfulness,
            "answer_relevance": answer_relevance,
            "context_precision": context_precision,
            "token_cost": token_cost
        })
        
    n = len(scores) if scores else 1
    aggregates = {
        "mean_faithfulness": sum(r["faithfulness"] for r in scores) / n,
        "mean_answer_relevance": sum(r["answer_relevance"] for r in scores) / n,
        "mean_context_precision": sum(r["context_precision"] for r in scores) / n,
        "total_token_cost": sum(r["token_cost"] for r in scores)
    }
    
    return {"scores": scores, "aggregates": aggregates}

def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
    fixture_file = os.path.join(repo_root, "bench/fixtures/ragas-queries.yaml")
    
    with open(fixture_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        
    result = evaluate_ragas(data.get("queries", []))
    
    # Write output
    out_dir = os.path.join(repo_root, "bench/results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "ragas.json")
    
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
        
    print("\n── RAGAS Triad Results ──")
    print(f"  Faithfulness (mean):       {result['aggregates']['mean_faithfulness']:.3f}")
    print(f"  Answer Relevance (mean):   {result['aggregates']['mean_answer_relevance']:.3f}")
    print(f"  Context Precision (mean):  {result['aggregates']['mean_context_precision']:.3f}")
    print(f"  Total Token Cost:          {result['aggregates']['total_token_cost']}")
    print(f"\nWritten to: {os.path.relpath(out_path, repo_root)}")

if __name__ == '__main__':
    main()
