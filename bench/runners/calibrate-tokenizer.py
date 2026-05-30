#!/usr/bin/env python3
"""
Python port of bench/runners/calibrate-tokenizer.ts
Calibrates the local tiktoken token counter against Claude-4 ground truth.
Saves results to bench/results/calibrate-tokenizer.json.
"""

import os
import sys
import json
import time
import math
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from lattice.util.tokens import count_tokens as local_count_tokens
from bench.lib.count_tokens import count_via_anthropic_api

MODEL = os.environ.get("LATTICE_BENCH_MODEL", "claude-opus-4-7")

# 30 samples sourced from real lattice files and shapes, identical to TS.
SAMPLES = [
    # --- prose (notes, design docs) ---
    {
        "id": "prose-1",
        "bucket": "prose",
        "description": "README justification paragraph",
        "text": "Across 52 surveyed tools, every option fails on at least one axis: tool-list bloat (Serena 29 tools, Notion 22, Letta 20), embedding-cost runaway (claude-context, Zep, mem0 default to OpenAI embeddings and silently bleed money), dump-all foot-guns (read_graph, aim_memory_read_all, vault dumps — the model can't resist them), fading memory (no freshness model, so confidently-wrong stored facts poison context after weeks), and compaction amnesia (Claude Code forgets your MCP tools after auto-compaction unless you hand-wire SessionStart hooks). lattice fixes all five."
    },
    {
        "id": "prose-2",
        "bucket": "prose",
        "description": "Architecture doc opening",
        "text": "This is the wiring diagram for new contributors. It explains how the four runtime parts (MCP server, hooks, vault, retrieval cascade) hand off to each other and why the boundaries are where they are. The seven entry points: lattice ships one MCP server, one CLI, and five hook scripts. They're independent processes; the only thing they share is the on-disk vault."
    },
    {
        "id": "prose-3",
        "bucket": "prose",
        "description": "Multi-paragraph design rationale",
        "text": "The freshness model is one of lattice's stated differentiators. Old memories don't outrank new ones, and superseded facts disappear from recall but stay accessible via getChunkRaw for audit. The decay function is exponential by source-specific tau: code_index never decays (it's ground truth), human notes decay at tau=180 days, auto-captures at tau=30 days. Source weight modulates the decay output: code=1.0, human=0.9, auto=0.6.\n\nThe weighted decay handles a real failure mode in long-running projects: a fresh auto-capture from yesterday's chat shouldn't outrank a year-old human-authored decision document, even if the auto-capture is technically more recent. The 0.6 weight on auto-captures bakes in the prior that they're noisy. Conversely, a fresh human note can outrank a very old human note from the same source: the decay says recency matters within a source, the weight says the source matters across sources. Both axes are needed."
    },
    {
        "id": "prose-4",
        "bucket": "prose",
        "description": "Decision-log markdown template",
        "text": "# Decision: [Title]\n\n**Date**: [ISO date]\n**Status**: accepted | proposed | deprecated\n**Supersedes**: [chunk_id or \"none\"]\n\n## Context\n\n[What is the situation that requires a decision?]\n\n## Decision\n\n[What was decided and why.]\n\n## Consequences\n\n- [Positive consequence]\n- [Trade-off or risk accepted]\n\n## Alternatives considered\n\n1. [Alternative A] — rejected because [reason]\n2. [Alternative B] — rejected because [reason]"
    },
    {
        "id": "prose-5",
        "bucket": "prose",
        "description": "Conversational FAQ-style explanation",
        "text": "Q: Why does lattice use SQLite FTS5 instead of vector embeddings as the default? A: Embeddings have three costs people underestimate: download size (33MB+ ONNX models), CPU cost on every recall, and tuning complexity. FTS5 covers 80% of practical retrieval — symbol lookups, exact-phrase matches, well-formed natural-language queries. The remaining 20% benefits from semantic matching, but for those queries the symbol-pass-then-BM25 cascade typically returns the right answer first anyway. Embeddings are opt-in via LATTICE_EMBEDDINGS=on for users who want the extra coverage."
    },
    {
        "id": "prose-6",
        "bucket": "prose",
        "description": "Friction-log entry style",
        "text": "Severity P1: PostToolUse hook crashed on .tsx edit. Root cause: web-tree-sitter was loading the .ts grammar for both .ts and .tsx files; .tsx requires the JSX-aware grammar variant. Reproduction: edit any .tsx file in a TypeScript project with lattice installed; observe hook log shows 'unexpected token <' parse error. Fix: route .tsx through tsx grammar, not ts. Status: fixed in commit 7f3a1b9. Adds explicit grammar selection in indexer/symbol.ts based on file extension."
    },
    # --- code (TS/python) ---
    {
        "id": "code-1",
        "bucket": "code",
        "description": "Short TS utility function",
        "text": "import { countTokens as anthropicCount } from \"@anthropic-ai/tokenizer\";\n\nexport function countTokens(s: string): number {\n  if (!s) return 0;\n  try {\n    return anthropicCount(s);\n  } catch {\n    return Math.ceil(s.length / 4);\n  }\n}\n\nexport function truncateToBudget(s: string, budget: number): string {\n  if (countTokens(s) <= budget) return s;\n  let lo = 0;\n  let hi = s.length;\n  while (lo < hi) {\n    const mid = (lo + hi + 1) >> 1;\n    if (countTokens(s.slice(0, mid)) <= budget - 1) lo = mid;\n    else hi = mid - 1;\n  }\n  return s.slice(0, lo) + \"…\";\n}"
    },
    {
        "id": "code-2",
        "bucket": "code",
        "description": "Vault open excerpt",
        "text": "export async function openVault(vaultDir: string): Promise<Vault> {\n  await fs.mkdir(path.join(vaultDir, \"notes\"), { recursive: true });\n  await fs.mkdir(path.join(vaultDir, \"log\"), { recursive: true });\n  const dbPath = path.join(vaultDir, \"index.db\");\n  const db = new Database(dbPath);\n  sqliteVec.load(db);\n  initSchema(db);\n\n  return {\n    dir: vaultDir,\n    db,\n    async getChunk(id: string) {\n      const row = db\n        .prepare(\"SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL\")\n        .get(id) as Chunk | undefined;\n      return row ?? null;\n    },\n    async getChunkRaw(id: string) {\n      const row = db.prepare(\"SELECT * FROM chunks WHERE id = ?\").get(id) as\n        | Chunk\n        | undefined;\n      return row ?? null;\n    },\n  };\n}"
    },
    {
        "id": "code-3",
        "bucket": "code",
        "description": "Cascade excerpt",
        "text": "  // Stage 1: exact symbol match (early exit)\n  if (args.kind !== \"notes\" && looksLikeIdentifier(args.query)) {\n    const hits = await lookupSymbol(vault, args.query, args.path_scope);\n    if (hits.length > 0) {\n      return packToBudget(hits, args.budget_tokens);\n    }\n  }\n\n  // Stage 2: parallel BM25 + semantic retrieval\n  const embeddingsOn = process.env.LATTICE_EMBEDDINGS === \"on\";\n  const sourceFilter = resolveSourceFilter(args.kind);\n\n  const [bm25Hits, semanticHits] = await Promise.all([\n    searchFts(vault, args.query, 15, args.path_scope, sourceFilter),\n    embeddingsOn\n      ? searchSemantic(vault, args.query, 10, args.path_scope)\n      : Promise.resolve([] as Chunk[]),\n  ]);\n\n  // Stage 3: RRF fusion + freshness rerank\n  const fused = rrfFuse([bm25Hits, semanticHits], args.since, sourceFilter);\n\n  // Stage 4: pack to token budget\n  return packToBudget(fused.map((f) => f.chunk), args.budget_tokens);"
    },
    {
        "id": "code-4",
        "bucket": "code",
        "description": "Vitest test block",
        "text": "describe(\"freshnessScore\", () => {\n  it(\"code_index has score=1.0 regardless of age\", () => {\n    const now = Date.now();\n    const oldChunk = makeChunk({\n      source: \"code_index\",\n      last_seen_at: new Date(now - 365 * DAY_MS).toISOString(),\n    });\n    expect(freshnessScore(oldChunk, now)).toBe(1.0);\n  });\n\n  it(\"human_note decays over time with tau=180d\", () => {\n    const now = Date.now();\n    const fresh = makeChunk({\n      source: \"human_note\",\n      last_seen_at: new Date(now).toISOString(),\n    });\n    const stale = makeChunk({\n      source: \"human_note\",\n      last_seen_at: new Date(now - 180 * DAY_MS).toISOString(),\n    });\n    expect(freshnessScore(fresh, now)).toBeGreaterThan(freshnessScore(stale, now));\n  });\n});"
    },
    {
        "id": "code-5",
        "bucket": "code",
        "description": "Python solve_dependencies function",
        "text": "async def solve_dependencies(\n    *,\n    request: Request,\n    dependant: Dependant,\n    body: Optional[Union[Dict[str, Any], FormData]] = None,\n    background_tasks: Optional[StarletteBackgroundTasks] = None,\n    response: Optional[Response] = None,\n    dependency_overrides_provider: Optional[Any] = None,\n    dependency_cache: Optional[Dict[Tuple[Callable[..., Any], Tuple[str]], Any]] = None,\n    async_exit_stack: AsyncExitStack,\n    embed_body_fields: bool,\n) -> SolvedDependency:\n    \"\"\"Resolve a dependency tree by walking the Dependant graph.\n\n    Caches resolved values to avoid recomputing shared sub-dependencies.\n    Returns SolvedDependency with values, errors, and accumulated background\n    tasks. Used internally by APIRoute.get_route_handler().\n    \"\"\"\n    values: Dict[str, Any] = {}\n    errors: List[Any] = []"
    },
    {
        "id": "code-6",
        "bucket": "code",
        "description": "TypeScript interfaces",
        "text": "export interface RecallArgs {\n  query: string;\n  budget_tokens: number;\n  kind: \"auto\" | \"code\" | \"notes\" | \"all\";\n  path_scope?: string;\n  since?: string;\n}\n\nexport interface RecallResult {\n  id: string;\n  heading: string;\n  preview: string;\n  freshness: number;\n  tokens: number;\n}\n\ntype StructuralMode = \"callers\" | \"imports\" | \"dependents\" | \"impl\";\n\ninterface ContinuationPayload {\n  offset: number;\n  query: string;\n  kind: string;\n}\n\nfunction encodeContinuation(payload: ContinuationPayload): string {\n  return Buffer.from(JSON.stringify(payload)).toString(\"base64url\");\n}"
    },
    # --- JSON-heavy ---
    {
        "id": "json-1",
        "bucket": "json",
        "description": "lattice.recall tool schema",
        "text": json.dumps({
            "name": "lattice.recall",
            "description": "Retrieve relevant project context (code symbols, notes, prior decisions) for a query.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language question OR a symbol identifier."},
                    "budget_tokens": {"type": "integer", "default": 2500, "minimum": 200, "maximum": 8000},
                    "kind": {"type": "string", "enum": ["auto", "code", "notes", "all"], "default": "auto"},
                    "path_scope": {"type": "string", "description": "Restrict retrieval to a subpath (monorepo scoping)."},
                    "since": {"type": "string", "description": "ISO date — only chunks last_seen after this."},
                    "continuation_token": {"type": "string", "description": "Token from a previous recall response."},
                },
                "required": ["query"],
            }
        })
    },
    {
        "id": "json-2",
        "bucket": "json",
        "description": "Bench-result metadata",
        "text": json.dumps({
            "meta": {
                "date": "2026-05-25T17:38:27.739Z",
                "lattice_sha": "uncommitted",
                "model": "claude-opus-4-7",
                "tokenizer_source": "claude-cli",
                "node": "22.20.0",
                "platform": "darwin-arm64",
            },
            "results": [
                {"server": "lattice", "status": "measured", "tools_count": 3, "total_tokens": 852},
                {"server": "serena", "status": "measured", "tools_count": 28, "total_tokens": 7750},
            ]
        })
    },
    {
        "id": "json-3",
        "bucket": "json",
        "description": "Serena find_symbol schema",
        "text": json.dumps({
            "name": "find_symbol",
            "description": "Performs a global search for symbols.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_path": {"type": "string", "description": "The name path (e.g. 'method')."},
                    "relative_path": {"type": "string", "description": "Optional subpath restriction."},
                    "depth": {"type": "integer", "default": 0},
                    "include_body": {"type": "boolean", "default": False},
                    "include_kinds": {"type": "array", "items": {"type": "integer"}},
                    "substring_matching": {"type": "boolean", "default": False},
                },
                "required": ["name_path"]
            }
        })
    },
    {
        "id": "json-4",
        "bucket": "json",
        "description": "Test scenarios JSON",
        "text": json.dumps({
            "results": {
                "scenarios": [
                    {"id": 1, "description": "decay ordering", "pass": True, "expected": "T-1d > T-7d > T-30d"},
                    {"id": 2, "description": "F2 supersedes F1", "pass": True, "expected": "results contain F2 NOT F1"},
                    {"id": 10, "description": "pinned outranks fresh", "pass": False, "reason": "feature_not_implemented"},
                ],
                "pass_count": 9,
                "total": 10
            }
        })
    },
    {
        "id": "json-5",
        "bucket": "json",
        "description": "package.json style",
        "text": json.dumps({
            "name": "lattice-mcp",
            "version": "0.1.0",
            "description": "Token-efficient hybrid retrieval",
            "main": "./dist/server.js",
            "scripts": {
                "build": "tsc -p tsconfig.json",
                "test": "vitest run"
            },
            "dependencies": {
                "better-sqlite3": "^11.0.0",
                "zod": "^3.23.0"
            }
        })
    },
    {
        "id": "json-6",
        "bucket": "json",
        "description": "OpenAPI fragment",
        "text": json.dumps({
            "openapi": "3.0.3",
            "paths": {
                "/items/{id}": {
                    "get": {
                        "summary": "Get item by ID",
                        "parameters": [
                            {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                        ],
                        "responses": {
                            "200": {"description": "OK"}
                        }
                    }
                }
            }
        })
    },
    # --- mixed (markdown + YAML frontmatter) ---
    {
        "id": "mixed-1",
        "bucket": "mixed",
        "description": "Filled-in decision log",
        "text": "# Decision: switch tokenizer to claude-cli for bench work\n\n**Date**: 2026-05-25\n**Status**: accepted\n**Supersedes**: none\n\n## Context\n\n@anthropic-ai/tokenizer undercounts Claude 4 by ~37% vs the claude -p ground truth.\n\n## Decision\n\nAll bench runners count via claude -p --output-format json.\n\n## Consequences\n\n- BENCH.md numbers are defensible\n- Each bench burns Pro plan quota"
    },
    {
        "id": "mixed-2",
        "bucket": "mixed",
        "description": "Recall-shaped response output",
        "text": "[chunk a3f2e91b] vault.ts: openVault constructor sequence\n  preview: \"async function openVault(vaultDir: string): Promise<Vault> { await fs.mkdir(...); const db = new Database(dbPath); }\"\n  freshness: 1.0  source: code_index  tokens: 47\n  budget: { used: 47, limit: 1500, remaining_chunks: 4 }"
    },
    {
        "id": "mixed-3",
        "bucket": "mixed",
        "description": "Frontmatter prefixed note",
        "text": "---\nid: 7e5c9d3a\nheading: tokenizer validation finding\ntags: [bench, tokenizer]\nsource: human_note\ncreated_at: 2026-05-25T16:43:19Z\n---\n\n@anthropic-ai/tokenizer undercounts Claude 4 by ~37% on lattice's tool schemas. Checked via delta-of-deltas: writeTool gave cli_total=20186, allTools=20961, delta=775 vs local delta of 488."
    },
    {
        "id": "mixed-4",
        "bucket": "mixed",
        "description": "Multi-section bench README",
        "text": "# bench/\n\nOperational benchmark suite for lattice. Spec lives in `../docs/benchmarking.md`.\n\n## Layout\n\n| Path | Purpose |\n|---|---|\n| `bench/fixture.txt` | Pinned fastapi commit |\n| `bench/runners/` | One file per benchmark |\n\n## Running\n\nEach runner is a python script:\n\n```bash\npython bench/runners/tool-overhead.py\n```"
    },
    {
        "id": "mixed-5",
        "bucket": "mixed",
        "description": "Inline code references in prose",
        "text": "The `recall()` tool packs results within `budget_tokens` using `truncateToBudget()` from `src/util/tokens.ts`. When the budget is exhausted, a `continuation_token` (base64url JSON of `{offset, query, kind}`) lets the caller paginate. Offset N is verified against original query parameter."
    },
    {
        "id": "mixed-6",
        "bucket": "mixed",
        "description": "Issue-tracker style entry",
        "text": "**Title**: PostToolUse hook hangs on large-file edits (>1MB)\n**Severity**: P2\n**Affects**: hooks/post-tool-use.js\n\nWhen Edit/Write touches a file larger than ~1MB, the hook's incremental re-index spends >5 seconds parsing tree-sitter. Larger files index lazily on next `lattice rebuild`."
    },
    # --- edge cases ---
    {
        "id": "edge-1",
        "bucket": "edge",
        "description": "Single ASCII character",
        "text": "x"
    },
    {
        "id": "edge-2",
        "bucket": "edge",
        "description": "Short sentence",
        "text": "The quick brown fox jumps over the lazy dog."
    },
    {
        "id": "edge-3",
        "bucket": "edge",
        "description": "Long repetitive pattern",
        "text": "lorem ipsum dolor sit amet " * 250
    },
    {
        "id": "edge-4",
        "bucket": "edge",
        "description": "Mixed unicode + emoji",
        "text": "Status: ✓ done. Deploy region: 東京 (Tokyo). Operator: Андрей. Notes: 🚀 launched 2026-05-25. Cost: ~$0.42 incl. 中文 chars."
    },
    {
        "id": "edge-5",
        "bucket": "edge",
        "description": "Very long English-shaped sentence repeats",
        "text": "The quick brown fox jumps over the lazy dog while the rain in Spain falls mainly on the plain. " * 62
    },
    {
        "id": "edge-6",
        "bucket": "edge",
        "description": "All numeric and structured edge cases",
        "text": "0.001 0.012 0.043 0.117 0.234 0.481 0.582 0.723 0.891 1.024 1.234 1.481 1.582 1.733 1.891 2.024 1234567890 9876543210 0xDEADBEEF 2026-05-26T11:38:14.782+05:30 1716729494"
    }
]

def percentile(arr, p):
    if not arr:
        return 0.0
    sorted_arr = sorted(arr)
    idx = min(len(sorted_arr) - 1, int(len(sorted_arr) * p))
    return sorted_arr[idx]

def median(arr):
    return percentile(arr, 0.5)

def main():
    started_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    print("→ counting via anthropic-api...", file=sys.stderr)
    
    print("→ baseline count for \"x\"...", file=sys.stderr)
    baseline_count = count_via_anthropic_api("x")
    print(f"  baseline_count = {baseline_count}\n", file=sys.stderr)

    print(f"→ counting {len(SAMPLES)} samples...", file=sys.stderr)
    t0 = time.time()
    
    # Run sequentially in python
    raw_counts = []
    for idx, s in enumerate(SAMPLES):
        count_val = count_via_anthropic_api(s["text"])
        raw_counts.append(count_val)
        print(f"  [{idx+1}/{len(SAMPLES)}] counted {s['id']}", file=sys.stderr)
        
    elapsed = time.time() - t0
    print(f"  done in {elapsed:.1f}s\n", file=sys.stderr)

    results = []
    for i, s in enumerate(SAMPLES):
        local_tokens = local_count_tokens(s["text"])
        api_count = raw_counts[i]
        # Subtract baseline framing to isolate pure content tokens (baseline_local = 1)
        claude4_tokens = api_count - baseline_count + 1
        ratio = claude4_tokens / local_tokens if local_tokens > 0 else 0.0
        
        results.append({
            "id": s["id"],
            "bucket": s["bucket"],
            "description": s["description"],
            "text_length_chars": len(s["text"]),
            "local_tokens": local_tokens,
            "claude4_tokens": claude4_tokens,
            "ratio": ratio,
            "raw_api_input_tokens": api_count
        })

    for r in results:
        print(f"  {r['id'].ljust(8)} {r['bucket'].ljust(6)} local={str(r['local_tokens']).rjust(5)} c4={str(r['claude4_tokens']).rjust(5)} ratio={r['ratio']:.3f}", file=sys.stderr)

    # Compute aggregate stats
    all_ratios = [r["ratio"] for r in results]
    overall = {
        "samples": len(results),
        "min_ratio": min(all_ratios),
        "median_ratio": median(all_ratios),
        "p95_ratio": percentile(all_ratios, 0.95),
        "max_ratio": max(all_ratios),
        "mean_ratio": sum(all_ratios) / len(all_ratios),
        "spread": max(all_ratios) - min(all_ratios)
    }

    buckets = {}
    for bucket in ["prose", "code", "json", "mixed", "edge"]:
        bucket_results = [r for r in results if r["bucket"] == bucket]
        ratios = [r["ratio"] for r in bucket_results]
        buckets[bucket] = {
            "count": len(bucket_results),
            "min_ratio": min(ratios) if ratios else 0,
            "median_ratio": median(ratios) if ratios else 0,
            "p95_ratio": percentile(ratios, 0.95) if ratios else 0,
            "max_ratio": max(ratios) if ratios else 0,
            "mean_ratio": sum(ratios) / len(ratios) if ratios else 0
        }

    non_edge = [r for r in results if r["bucket"] != "edge"]
    non_edge_ratios = [r["ratio"] for r in non_edge]
    overall_non_edge = {
        "samples": len(non_edge),
        "min_ratio": min(non_edge_ratios) if non_edge_ratios else 0,
        "median_ratio": median(non_edge_ratios) if non_edge_ratios else 0,
        "p95_ratio": percentile(non_edge_ratios, 0.95) if non_edge_ratios else 0,
        "max_ratio": max(non_edge_ratios) if non_edge_ratios else 0,
        "spread": (max(non_edge_ratios) - min(non_edge_ratios)) if non_edge_ratios else 0
    }

    length_scatter = sorted(
        [{"id": r["id"], "bucket": r["bucket"], "local_tokens": r["local_tokens"], "ratio": r["ratio"]} for r in results],
        key=lambda x: x["local_tokens"]
    )
    bucket_scatter = sorted(
        [{"id": r["id"], "bucket": r["bucket"], "local_tokens": r["local_tokens"], "ratio": r["ratio"]} for r in results],
        key=lambda x: (x["bucket"], x["local_tokens"])
    )

    recommendations = {
        "p95_overall": math.ceil(overall["p95_ratio"] * 100) / 100,
        "max_times_1_05_overall": math.ceil(overall["max_ratio"] * 1.05 * 100) / 100,
        "p95_non_edge": math.ceil(overall_non_edge["p95_ratio"] * 100) / 100,
        "max_times_1_05_non_edge": math.ceil(overall_non_edge["max_ratio"] * 1.05 * 100) / 100
    }

    output = {
        "meta": {
            "date": started_at,
            "tokenizer_under_test": "tiktoken",
            "ground_truth_source": "anthropic-api",
            "model": MODEL,
            "python": sys.version,
            "platform": sys.platform,
            "sample_count": len(SAMPLES),
            "baseline": {
                "text": "x",
                "local_tokens": 1,
                "api_input_tokens": baseline_count
            }
        },
        "samples": results,
        "statistics": {
            "overall": overall,
            "overall_non_edge": overall_non_edge,
            "by_bucket": buckets
        },
        "scatter": {
            "length_x_ratio": length_scatter,
            "bucket_x_ratio": bucket_scatter
        },
        "recommendations": recommendations
    }

    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../results'))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "calibrate-tokenizer.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"\n→ wrote {os.path.relpath(out_path, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))}", file=sys.stderr)
    print(f"\noverall (n={overall['samples']}):", file=sys.stderr)
    print(f"  median={overall['median_ratio']:.3f} P95={overall['p95_ratio']:.3f} max={overall['max_ratio']:.3f} spread={overall['spread']:.3f}", file=sys.stderr)
    print(f"non-edge (n={overall_non_edge['samples']}):", file=sys.stderr)
    print(f"  median={overall_non_edge['median_ratio']:.3f} P95={overall_non_edge['p95_ratio']:.3f} max={overall_non_edge['max_ratio']:.3f} spread={overall_non_edge['spread']:.3f}\n", file=sys.stderr)
    print("recommendation candidates:", file=sys.stderr)
    print(f"  P95-of-30        (overall)  = {recommendations['p95_overall']}", file=sys.stderr)
    print(f"  max x 1.05       (overall)  = {recommendations['max_times_1_05_overall']}", file=sys.stderr)
    print(f"  P95-of-24        (non-edge) = {recommendations['p95_non_edge']}", file=sys.stderr)
    print(f"  max x 1.05       (non-edge) = {recommendations['max_times_1_05_non_edge']}", file=sys.stderr)

if __name__ == '__main__':
    main()
