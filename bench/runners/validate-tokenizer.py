#!/usr/bin/env python3
"""
Python port of bench/runners/validate-tokenizer.ts
Validates the local tiktoken token counter against Claude CLI ground truth.
Writes JSON to bench/results/tokenizer-validation.json.
"""

import os
import sys
import json
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from lattice.util.tokens import count_tokens as local_count_tokens
from lattice.tools.recall import RecallArgs
from lattice.tools.recall_expand import RecallExpandArgs
from lattice.tools.write import WriteArgs
from bench.lib.count_tokens import count_via_anthropic_api

MODEL = os.environ.get("LATTICE_BENCH_MODEL", "claude-opus-4-7")

# Construct the API tool definitions using Pydantic schemas
TOOLS = [
    {
        "name": "recall",
        "description": "Retrieve relevant project context (code symbols, notes, prior decisions) for a query.",
        "input_schema": RecallArgs.model_json_schema()
    },
    {
        "name": "recall_expand",
        "description": "Expand a chunk by ID or explore its relations (callers, imports, etc.).",
        "input_schema": RecallExpandArgs.model_json_schema()
    },
    {
        "name": "write",
        "description": "Persist a fact, decision, or note. One focused statement per call.",
        "input_schema": WriteArgs.model_json_schema()
    }
]

def pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return round(((a - b) / b) * 100, 1)

def main():
    started_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    
    # 1. Local counts
    per_tool = []
    for t in TOOLS:
        json_str = json.dumps(t)
        per_tool.append({
            "name": t["name"],
            "json_chars": len(json_str),
            "local_tokens": local_count_tokens(json_str),
            "api_tokens": None,
            "diff_pct_local_vs_api": None
        })
        
    local_total = sum(t["local_tokens"] for t in per_tool)
    
    # 2. Ground-truth via Claude CLI delta-of-deltas
    # Baseline: write tool (smallest schema)
    write_tool_schema = next(t for t in TOOLS if t["name"] == "write")
    baseline_text = json.dumps(write_tool_schema)
    baseline_local = local_count_tokens(baseline_text)
    
    # With content: all three tools
    with_content_text = json.dumps(TOOLS)
    with_content_local = local_count_tokens(with_content_text)
    
    ground_total = None
    total_diff_pct = None
    ground_error = None
    ground_truth_block = {"source": "anthropic-api"}
    
    try:
        print(f"→ baseline call (write, {baseline_local} local tokens)...", file=sys.stderr)
        baseline_api = count_via_anthropic_api(baseline_text)
        
        print(f"→ with-content call (all 3 tools, {with_content_local} local tokens)...", file=sys.stderr)
        with_content_api = count_via_anthropic_api(with_content_text)
        
        api_delta = with_content_api - baseline_api
        local_delta = with_content_local - baseline_local
        
        ground_total = api_delta
        total_diff_pct = pct_diff(local_delta, api_delta)
        
        # Attribute proportional counts for reporting
        for t in per_tool:
            prop_api = int(round((t["local_tokens"] / local_total) * api_delta))
            t["api_tokens"] = prop_api
            t["diff_pct_local_vs_api"] = pct_diff(t["local_tokens"], prop_api)
            
        ground_truth_block = {
            "source": "anthropic-api",
            "endpoint": "POST /v1/messages/count_tokens",
            "baseline": {
                "prompt": "JSON.stringify(write)",
                "local_tokens": baseline_local,
                "api_total_tokens": baseline_api
            },
            "with_content": {
                "prompt": "JSON.stringify([recall, recall_expand, write])",
                "local_tokens": with_content_local,
                "api_total_tokens": with_content_api
            },
            "local_delta": local_delta,
            "api_delta": api_delta,
            "total_diff_pct_local_vs_api": total_diff_pct,
            "error": None
        }
        
    except Exception as e:
        ground_error = str(e)
        ground_truth_block = {
            "source": "anthropic-api",
            "error": ground_error
        }
        
    # 3. Verdict
    if ground_error:
        verdict = f"anthropic-api error: {ground_error}"
    elif total_diff_pct is None or ground_total is None:
        verdict = "incomplete: ground-truth counts missing"
    elif abs(total_diff_pct) > 5:
        verdict = (
            f"local tiktoken differs by {total_diff_pct}% from anthropic-api ground-truth "
            f"(local={local_total}, api={ground_total}) — SWITCH to anthropic-api for all bench work."
        )
    else:
        verdict = (
            f"local tiktoken within 5% of anthropic-api ground-truth ({total_diff_pct}%) — "
            f"local tokenizer is acceptable for bench work."
        )
        
    result = {
        "meta": {
            "date": started_at,
            "model": MODEL,
            "python": sys.version,
            "platform": sys.platform,
            "tokenizer_source": "anthropic-api"
        },
        "local": {
            "tokenizer": "tiktoken",
            "total_tokens": local_total,
            "per_tool": per_tool
        },
        "ground_truth": ground_truth_block,
        "verdict": verdict
    }
    
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../results'))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "tokenizer-validation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
        
    print(json.dumps(result, indent=2))
    print(f"\n→ wrote {os.path.relpath(out_path, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))}", file=sys.stderr)
    print(f"→ {verdict}", file=sys.stderr)
    
    if ground_error:
        sys.exit(2)

if __name__ == '__main__':
    main()
