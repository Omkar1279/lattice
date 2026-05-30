#!/usr/bin/env tsx
/**
 * Calibrate the local @anthropic-ai/tokenizer against Claude-4 ground truth.
 *
 * Method (revised after methodology critique):
 *   1. 30 samples across 5 buckets (6 per bucket).
 *   2. Counting path: Anthropic /v1/messages/count_tokens (exact, free,
 *      parallelisable). Falls back to claude -p if no .anthropic-key.
 *   3. Per sample, compute claude4_tokens by subtracting the baseline
 *      ("x" → 1 local token) from the API's input_tokens. The API includes
 *      a constant framing overhead (~7-13 tokens) that cancels in the delta.
 *   4. Aggregate: P50, P95-of-30, max overall, plus per-bucket and
 *      length-scatter. Recommendation reports BOTH:
 *        - P95-of-30 ratio  (statistical 95th percentile)
 *        - max × 1.05       (more defensible at small n; ceiling above the
 *                            empirical worst case)
 *      The user picks based on the spread.
 *   5. Two scatter plots emitted for the human reviewer:
 *        - length × ratio (super-linear drift check)
 *        - bucket × ratio (content-type drift check)
 *
 * Output: bench/results/<YYYY-MM-DD>/calibrate-tokenizer.json
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { countTokens as anthropicLocalCount } from "@anthropic-ai/tokenizer";
import {
  countViaAnthropicApi,
  anthropicKeyAvailable,
} from "../lib/count-tokens-api.js";
import { countViaClaudeCli } from "../lib/count-tokens-cli.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

// ────────────────────────────────────────────────────────────────────────────
// Sample corpus — 30 entries, 6 per bucket. Sourced from real lattice files
// and conventional content shapes, never invented or LLM-generated.
// ────────────────────────────────────────────────────────────────────────────

type Bucket = "prose" | "code" | "json" | "mixed" | "edge";

interface Sample {
  id: string;
  bucket: Bucket;
  description: string;
  text: string;
}

const repeatPattern = (pattern: string, repeats: number) =>
  Array(repeats).fill(pattern).join("");

const SAMPLES: Sample[] = [
  // ── prose (notes, design docs) — 6 ───────────────────────────────────────
  {
    id: "prose-1",
    bucket: "prose",
    description: "README justification paragraph",
    text:
      "Across 52 surveyed tools, every option fails on at least one axis: tool-list bloat (Serena 29 tools, Notion 22, Letta 20), embedding-cost runaway (claude-context, Zep, mem0 default to OpenAI embeddings and silently bleed money), dump-all foot-guns (read_graph, aim_memory_read_all, vault dumps — the model can't resist them), fading memory (no freshness model, so confidently-wrong stored facts poison context after weeks), and compaction amnesia (Claude Code forgets your MCP tools after auto-compaction unless you hand-wire SessionStart hooks). lattice fixes all five.",
  },
  {
    id: "prose-2",
    bucket: "prose",
    description: "Architecture doc opening",
    text:
      "This is the wiring diagram for new contributors. It explains how the four runtime parts (MCP server, hooks, vault, retrieval cascade) hand off to each other and why the boundaries are where they are. The seven entry points: lattice ships one MCP server, one CLI, and five hook scripts. They're independent processes; the only thing they share is the on-disk vault.",
  },
  {
    id: "prose-3",
    bucket: "prose",
    description: "Multi-paragraph design rationale",
    text:
      "The freshness model is one of lattice's stated differentiators. Old memories don't outrank new ones, and superseded facts disappear from recall but stay accessible via getChunkRaw for audit. The decay function is exponential by source-specific tau: code_index never decays (it's ground truth), human notes decay at tau=180 days, auto-captures at tau=30 days. Source weight modulates the decay output: code=1.0, human=0.9, auto=0.6.\n\nThe weighted decay handles a real failure mode in long-running projects: a fresh auto-capture from yesterday's chat shouldn't outrank a year-old human-authored decision document, even if the auto-capture is technically more recent. The 0.6 weight on auto-captures bakes in the prior that they're noisy. Conversely, a fresh human note can outrank a very old human note from the same source: the decay says recency matters within a source, the weight says the source matters across sources. Both axes are needed.",
  },
  {
    id: "prose-4",
    bucket: "prose",
    description: "Decision-log markdown template (sparse markdown structure)",
    text:
      "# Decision: [Title]\n\n**Date**: [ISO date]\n**Status**: accepted | proposed | deprecated\n**Supersedes**: [chunk_id or \"none\"]\n\n## Context\n\n[What is the situation that requires a decision?]\n\n## Decision\n\n[What was decided and why.]\n\n## Consequences\n\n- [Positive consequence]\n- [Trade-off or risk accepted]\n\n## Alternatives considered\n\n1. [Alternative A] — rejected because [reason]\n2. [Alternative B] — rejected because [reason]",
  },
  {
    id: "prose-5",
    bucket: "prose",
    description: "Conversational technical explanation (FAQ-style)",
    text:
      "Q: Why does lattice use SQLite FTS5 instead of vector embeddings as the default? A: Embeddings have three costs people underestimate: download size (33MB+ ONNX models), CPU cost on every recall, and tuning complexity. FTS5 covers 80% of practical retrieval — symbol lookups, exact-phrase matches, well-formed natural-language queries. The remaining 20% benefits from semantic matching, but for those queries the symbol-pass-then-BM25 cascade typically returns the right answer first anyway. Embeddings are opt-in via LATTICE_EMBEDDINGS=on for users who want the extra coverage.",
  },
  {
    id: "prose-6",
    bucket: "prose",
    description: "Friction-log entry style — bug report prose",
    text:
      "Severity P1: PostToolUse hook crashed on .tsx edit. Root cause: web-tree-sitter was loading the .ts grammar for both .ts and .tsx files; .tsx requires the JSX-aware grammar variant. Reproduction: edit any .tsx file in a TypeScript project with lattice installed; observe hook log shows 'unexpected token <' parse error. Fix: route .tsx through tsx grammar, not ts. Status: fixed in commit 7f3a1b9. Adds explicit grammar selection in indexer/symbol.ts based on file extension.",
  },

  // ── code (TS from src/) — 6 ──────────────────────────────────────────────
  {
    id: "code-1",
    bucket: "code",
    description: "src/util/tokens.ts countTokens — small TS function",
    text:
      "import { countTokens as anthropicCount } from \"@anthropic-ai/tokenizer\";\n\nexport function countTokens(s: string): number {\n  if (!s) return 0;\n  try {\n    return anthropicCount(s);\n  } catch {\n    return Math.ceil(s.length / 4);\n  }\n}\n\nexport function truncateToBudget(s: string, budget: number): string {\n  if (countTokens(s) <= budget) return s;\n  let lo = 0;\n  let hi = s.length;\n  while (lo < hi) {\n    const mid = (lo + hi + 1) >> 1;\n    if (countTokens(s.slice(0, mid)) <= budget - 1) lo = mid;\n    else hi = mid - 1;\n  }\n  return s.slice(0, lo) + \"…\";\n}",
  },
  {
    id: "code-2",
    bucket: "code",
    description: "src/storage/vault.ts excerpt — vault impl with SQL + comments",
    text:
      "export async function openVault(vaultDir: string): Promise<Vault> {\n  await fs.mkdir(path.join(vaultDir, \"notes\"), { recursive: true });\n  await fs.mkdir(path.join(vaultDir, \"log\"), { recursive: true });\n  const dbPath = path.join(vaultDir, \"index.db\");\n  const db = new Database(dbPath);\n  sqliteVec.load(db);\n  initSchema(db);\n\n  return {\n    dir: vaultDir,\n    db,\n    async getChunk(id: string) {\n      // Hides superseded chunks so they can't sneak back in via the symbol\n      // early-exit in cascade.ts. recall_expand still needs them for audit\n      // and reaches them via getChunkRaw().\n      const row = db\n        .prepare(\"SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL\")\n        .get(id) as Chunk | undefined;\n      return row ?? null;\n    },\n    async getChunkRaw(id: string) {\n      const row = db.prepare(\"SELECT * FROM chunks WHERE id = ?\").get(id) as\n        | Chunk\n        | undefined;\n      return row ?? null;\n    },\n  };\n}",
  },
  {
    id: "code-3",
    bucket: "code",
    description: "src/retrieval/cascade.ts excerpt — multi-stage cascade",
    text:
      "  // Stage 1: exact symbol match (early exit)\n  if (args.kind !== \"notes\" && looksLikeIdentifier(args.query)) {\n    const hits = await lookupSymbol(vault, args.query, args.path_scope);\n    if (hits.length > 0) {\n      return packToBudget(hits, args.budget_tokens);\n    }\n  }\n\n  // Stage 2: parallel BM25 + semantic retrieval\n  const embeddingsOn = process.env.LATTICE_EMBEDDINGS === \"on\";\n  const sourceFilter = resolveSourceFilter(args.kind);\n\n  const [bm25Hits, semanticHits] = await Promise.all([\n    searchFts(vault, args.query, 15, args.path_scope, sourceFilter),\n    embeddingsOn\n      ? searchSemantic(vault, args.query, 10, args.path_scope)\n      : Promise.resolve([] as Chunk[]),\n  ]);\n\n  // Stage 3: RRF fusion + freshness rerank\n  const fused = rrfFuse([bm25Hits, semanticHits], args.since, sourceFilter);\n\n  // Stage 4: pack to token budget\n  return packToBudget(fused.map((f) => f.chunk), args.budget_tokens);",
  },
  {
    id: "code-4",
    bucket: "code",
    description: "Vitest test — typical assertion-heavy code",
    text:
      "describe(\"freshnessScore\", () => {\n  it(\"code_index has score=1.0 regardless of age\", () => {\n    const now = Date.now();\n    const oldChunk = makeChunk({\n      source: \"code_index\",\n      last_seen_at: new Date(now - 365 * DAY_MS).toISOString(),\n    });\n    expect(freshnessScore(oldChunk, now)).toBe(1.0);\n  });\n\n  it(\"human_note decays over time with tau=180d\", () => {\n    const now = Date.now();\n    const fresh = makeChunk({\n      source: \"human_note\",\n      last_seen_at: new Date(now).toISOString(),\n    });\n    const stale = makeChunk({\n      source: \"human_note\",\n      last_seen_at: new Date(now - 180 * DAY_MS).toISOString(),\n    });\n    expect(freshnessScore(fresh, now)).toBeGreaterThan(freshnessScore(stale, now));\n  });\n});",
  },
  {
    id: "code-5",
    bucket: "code",
    description: "Python function with docstring (different language)",
    text:
      "async def solve_dependencies(\n    *,\n    request: Request,\n    dependant: Dependant,\n    body: Optional[Union[Dict[str, Any], FormData]] = None,\n    background_tasks: Optional[StarletteBackgroundTasks] = None,\n    response: Optional[Response] = None,\n    dependency_overrides_provider: Optional[Any] = None,\n    dependency_cache: Optional[Dict[Tuple[Callable[..., Any], Tuple[str]], Any]] = None,\n    async_exit_stack: AsyncExitStack,\n    embed_body_fields: bool,\n) -> SolvedDependency:\n    \"\"\"Resolve a dependency tree by walking the Dependant graph.\n\n    Caches resolved values to avoid recomputing shared sub-dependencies.\n    Returns SolvedDependency with values, errors, and accumulated background\n    tasks. Used internally by APIRoute.get_route_handler().\n    \"\"\"\n    values: Dict[str, Any] = {}\n    errors: List[Any] = []",
  },
  {
    id: "code-6",
    bucket: "code",
    description: "TypeScript with rich type annotations",
    text:
      "export interface RecallArgs {\n  query: string;\n  budget_tokens: number;\n  kind: \"auto\" | \"code\" | \"notes\" | \"all\";\n  path_scope?: string;\n  since?: string;\n}\n\nexport interface RecallResult {\n  id: string;\n  heading: string;\n  preview: string;\n  freshness: number;\n  tokens: number;\n}\n\ntype StructuralMode = \"callers\" | \"imports\" | \"dependents\" | \"impl\";\n\ninterface ContinuationPayload {\n  offset: number;\n  query: string;\n  kind: string;\n}\n\nfunction encodeContinuation(payload: ContinuationPayload): string {\n  return Buffer.from(JSON.stringify(payload)).toString(\"base64url\");\n}",
  },

  // ── JSON-heavy (schemas, structured data) — 6 ────────────────────────────
  {
    id: "json-1",
    bucket: "json",
    description: "lattice.recall MCP tool schema",
    text: JSON.stringify({
      name: "lattice.recall",
      description:
        "Retrieve relevant project context (code symbols, notes, prior decisions) for a query. Returns headings + 2-line previews; call lattice.recall_expand for full body of one chunk. Token budget is enforced server-side. Use continuation_token to paginate beyond the budget window.",
      input_schema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Natural-language question OR a symbol identifier." },
          budget_tokens: { type: "integer", default: 2500, minimum: 200, maximum: 8000 },
          kind: { type: "string", enum: ["auto", "code", "notes", "all"], default: "auto" },
          path_scope: { type: "string", description: "Restrict retrieval to a subpath (monorepo scoping)." },
          since: { type: "string", description: "ISO date — only chunks last_seen after this." },
          continuation_token: { type: "string", description: "Token from a previous recall response to fetch the next page of results." },
        },
        required: ["query"],
      },
    }),
  },
  {
    id: "json-2",
    bucket: "json",
    description: "Bench-result excerpt — meta block + small results array",
    text: JSON.stringify({
      meta: {
        date: "2026-05-25T17:38:27.739Z",
        lattice_sha: "uncommitted",
        model: "claude-opus-4-7",
        tokenizer_source: "claude-cli",
        node: "22.20.0",
        platform: "darwin-arm64",
      },
      results: [
        { server: "lattice", status: "measured", tools_count: 3, total_tokens: 852 },
        { server: "serena", status: "measured", tools_count: 28, total_tokens: 7750 },
      ],
    }),
  },
  {
    id: "json-3",
    bucket: "json",
    description: "Serena find_symbol schema — large JSON",
    text: JSON.stringify({
      name: "find_symbol",
      description:
        "Performs a global (or local) search for symbols with a given name/substring (optionally filtered by type). Returns symbols with their full path, kind, location, and (optionally) body. The path used to identify the symbol uses '/' as separator. The path can either be absolute (starting with '/') for matching from the project root, or relative (not starting with '/') for matching anywhere within the path of any file in the project.",
      input_schema: {
        type: "object",
        properties: {
          name_path: { type: "string", description: "The name path (e.g. 'method' or 'Class/method')." },
          relative_path: { type: "string", description: "Optional path to restrict search to a file or directory." },
          depth: { type: "integer", default: 0, description: "Depth of children to include for each matched symbol." },
          include_body: { type: "boolean", default: false, description: "Whether to include the full body of each symbol." },
          include_kinds: { type: "array", items: { type: "integer" }, description: "Filter by LSP SymbolKind integers." },
          exclude_kinds: { type: "array", items: { type: "integer" }, description: "SymbolKind integers to exclude." },
          substring_matching: { type: "boolean", default: false },
          max_answer_chars: { type: "integer", default: 200000 },
        },
        required: ["name_path"],
      },
    }),
  },
  {
    id: "json-4",
    bucket: "json",
    description: "Deeply-nested mixed-type JSON (test scenarios)",
    text: JSON.stringify({
      results: {
        scenarios: [
          { id: 1, description: "decay ordering", pass: true, expected: "T-1d > T-7d > T-30d", actual: "T-1d > T-7d > T-30d" },
          { id: 2, description: "F2 supersedes F1 → F1 omitted", pass: true, expected: "results contain F2 NOT F1", actual: "results = [F2]" },
          { id: 10, description: "pinned outranks fresh unpinned", pass: false, expected: "pinned wins", actual: "feature not implemented", reason: "feature_not_implemented" },
        ],
        pass_count: 9,
        total: 10,
      },
    }),
  },
  {
    id: "json-5",
    bucket: "json",
    description: "Package.json-style metadata",
    text: JSON.stringify({
      name: "lattice-mcp",
      version: "0.1.0",
      description: "Token-efficient hybrid retrieval and freshness-aware memory plugin for Claude Code. 3 MCP tools. Hooks-first. Local-only.",
      main: "./dist/server.js",
      bin: { lattice: "./bin/lattice.js" },
      scripts: {
        build: "tsc -p tsconfig.json",
        test: "vitest run",
        "bench:tool-overhead": "npm run build --silent && tsx bench/runners/tool-overhead.ts",
      },
      dependencies: {
        "@anthropic-ai/tokenizer": "^0.0.4",
        "better-sqlite3": "^11.0.0",
        zod: "^3.23.0",
      },
    }),
  },
  {
    id: "json-6",
    bucket: "json",
    description: "OpenAPI-style schema fragment",
    text: JSON.stringify({
      openapi: "3.0.3",
      paths: {
        "/items/{id}": {
          get: {
            summary: "Get item by ID",
            parameters: [
              { name: "id", in: "path", required: true, schema: { type: "integer", format: "int64" } },
              { name: "include", in: "query", schema: { type: "array", items: { type: "string" } } },
            ],
            responses: {
              "200": { description: "OK", content: { "application/json": { schema: { $ref: "#/components/schemas/Item" } } } },
              "404": { description: "Not Found" },
              "422": { description: "Validation Error" },
            },
          },
        },
      },
    }),
  },

  // ── mixed (recall-shaped responses, frontmatter) — 6 ─────────────────────
  {
    id: "mixed-1",
    bucket: "mixed",
    description: "Filled-in decision log: markdown structure + real prose",
    text:
      "# Decision: switch tokenizer to claude-cli for bench work\n\n**Date**: 2026-05-25\n**Status**: accepted\n**Supersedes**: none\n\n## Context\n\n@anthropic-ai/tokenizer ships the Claude-1-era BPE. §3 Step 0 measured a 37% undercount on lattice's tool schemas vs the claude -p ground truth. The local tokenizer is unsafe for any published bench number.\n\n## Decision\n\nAll bench runners count via `claude -p --output-format json` (or count_tokens API when ANTHROPIC_API_KEY is set). meta.tokenizer documents which.\n\n## Consequences\n\n- BENCH.md numbers are defensible against tokenizer-pedant critique\n- Each bench burns Pro plan quota for counting, not free metering\n- src/util/tokens.ts production undercount remains a separate fix",
  },
  {
    id: "mixed-2",
    bucket: "mixed",
    description: "Recall-shaped response: heading + preview + meta",
    text:
      "[chunk a3f2e91b] vault.ts: openVault constructor sequence\n  preview: \"async function openVault(vaultDir: string): Promise<Vault> { await fs.mkdir(...); const db = new Database(dbPath); sqliteVec.load(db); initSchema(db); return { dir: vaultDir, db, ... }; }\"\n  freshness: 1.0  source: code_index  tokens: 47\n  budget: { used: 47, limit: 1500, remaining_chunks: 4 }",
  },
  {
    id: "mixed-3",
    bucket: "mixed",
    description: "Frontmatter-prefixed note — YAML header + markdown body",
    text:
      "---\nid: 7e5c9d3a\nheading: tokenizer validation finding\ntags: [bench, tokenizer, decision]\nsource: human_note\ncreated_at: 2026-05-25T16:43:19Z\nlast_seen_at: 2026-05-25T16:43:19Z\nsupersedes: null\n---\n\n@anthropic-ai/tokenizer undercounts Claude 4 by ~37% on lattice's tool schemas. Verified via two delta-of-deltas measurements through `claude -p --output-format json`: writeTool JSON gave cli_total=20186, allTools JSON gave cli_total=20961, delta=775 vs local-tokenizer delta of 488. Switch all bench work to the validated path.",
  },
  {
    id: "mixed-4",
    bucket: "mixed",
    description: "Multi-section bench README — markdown + tables + paths",
    text:
      "# bench/\n\nOperational benchmark suite for lattice. Spec lives in `../docs/benchmarking.md`. Result JSONs are committed to this tree — the diff history is the audit trail.\n\n## Layout\n\n| Path | Purpose |\n|---|---|\n| `bench/fixture.txt` | Pinned commit SHA of the fastapi test repo |\n| `bench/fixtures/queries.yaml` | Ground-truth query set for Bench 2 |\n| `bench/runners/` | One file per benchmark |\n| `bench/results/YYYY-MM-DD/` | One directory per run |\n\n## Running\n\nEach runner is a `tsx` script. From the repo root:\n\n```bash\nnpm run bench:tool-overhead\nnpm run bench:freshness\nnpm run bench:validate-tokenizer\n```",
  },
  {
    id: "mixed-5",
    bucket: "mixed",
    description: "Inline code references in prose (typical doc style)",
    text:
      "The `recall()` tool packs results within `budget_tokens` using `truncateToBudget()` from `src/util/tokens.ts`. When the budget is exhausted mid-result, a `continuation_token` (base64url JSON of `{offset, query, kind}`) lets the caller paginate. The token is rejected on (query, kind) mismatch — applying offset N from query A onto query B's result list would silently skip rows. See `test/continuation.test.ts` for the invariant tests covering this case.",
  },
  {
    id: "mixed-6",
    bucket: "mixed",
    description: "Issue-tracker style entry — multi-paragraph + structured fields",
    text:
      "**Title**: PostToolUse hook hangs on large-file edits (>1MB)\n**Severity**: P2\n**Affects**: hooks/post-tool-use.js\n**First seen**: 2026-05-24\n\nWhen Edit/Write touches a file larger than ~1MB, the hook's incremental re-index spends >5 seconds parsing tree-sitter, which Claude Code reports as a stuck hook. Hook eventually completes but the user sees the orange spinner for an unreasonably long time.\n\n**Workaround**: skip files >LATTICE_MAX_FILE_BYTES in the hook (default suggested: 256KB). Larger files index lazily on next `lattice rebuild`.\n\n**Root cause**: tree-sitter's whole-file parse is O(n) but the constant is high enough to dominate for large files.",
  },

  // ── edge cases — 6 ──────────────────────────────────────────────────────
  {
    id: "edge-1",
    bucket: "edge",
    description: "Single ASCII character — minimum boundary",
    text: "x",
  },
  {
    id: "edge-2",
    bucket: "edge",
    description: "Short sentence — ~10 tokens",
    text: "The quick brown fox jumps over the lazy dog.",
  },
  {
    id: "edge-3",
    bucket: "edge",
    description: "Long repetitive pattern — ~3000 chars, BPE-friendly",
    text: repeatPattern("lorem ipsum dolor sit amet ", 250),
  },
  {
    id: "edge-4",
    bucket: "edge",
    description: "Mixed unicode — ASCII + emoji + CJK + Cyrillic",
    text:
      "Status: ✓ done. Deploy region: 東京 (Tokyo). Operator: Андрей. Notes: 🚀 launched 2026-05-25. " +
      "Errors caught: ❌ × 3, retried ↻. Pipeline: A → B → C ⇒ ✅. " +
      "Cost: ~$0.42 incl. 中文 chars. Token est. wobble across scripts is the test target.",
  },
  {
    id: "edge-5",
    bucket: "edge",
    description: "Very long content — ~6000 chars, randomised English-shaped text",
    text: repeatPattern(
      "The quick brown fox jumps over the lazy dog while the rain in Spain falls mainly on the plain. ",
      62
    ),
  },
  {
    id: "edge-6",
    bucket: "edge",
    description: "All-numeric / structured data without prose",
    text:
      "0.001 0.012 0.043 0.117 0.234 0.481 0.582 0.723 0.891 1.024 1.234 1.481 1.582 1.733 1.891 2.024 " +
      "1234567890 9876543210 0xDEADBEEF 0x0001 0xFFFF 0x1234 192.168.1.1 10.0.0.1 255.255.255.0 " +
      "2026-05-26T11:38:14.782+05:30 2026-05-25T17:38:27.739Z 1716729494 1716729600 " +
      "Decimal,Hex,IP,Timestamp — all common token-fragmentation patterns",
  },
];

// ────────────────────────────────────────────────────────────────────────────
// Statistics helpers
// ────────────────────────────────────────────────────────────────────────────

function percentile(arr: number[], p: number): number {
  if (arr.length === 0) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * p));
  return sorted[idx];
}

const median = (arr: number[]) => percentile(arr, 0.5);

// ────────────────────────────────────────────────────────────────────────────
// Main
// ────────────────────────────────────────────────────────────────────────────

interface SampleResult {
  id: string;
  bucket: Bucket;
  description: string;
  text_length_chars: number;
  local_tokens: number;
  claude4_tokens: number;
  ratio: number;
  raw_api_input_tokens: number;
}

interface BucketStats {
  count: number;
  min_ratio: number;
  median_ratio: number;
  p95_ratio: number;
  max_ratio: number;
  mean_ratio: number;
}

async function main() {
  const startedAt = new Date().toISOString();
  const apiAvailable = await anthropicKeyAvailable();
  const tokenizerSource = apiAvailable
    ? "anthropic-count-tokens-api"
    : "claude-cli";

  const count = apiAvailable
    ? (text: string) => countViaAnthropicApi(text)
    : (text: string) => countViaClaudeCli(text);

  process.stderr.write(`→ counting via ${tokenizerSource}\n`);

  // Baseline: count "x" once. The API includes a constant framing overhead
  // (~7-13 tokens) for each user message; subtracting baseline isolates
  // the content tokens. For "x" we approximate Claude-4 tokens = 1.
  process.stderr.write(`→ baseline count for "x"...\n`);
  const baselineCount = await count("x");
  process.stderr.write(`  baseline_count = ${baselineCount}\n\n`);

  // Run all sample counts in parallel — API supports it; local CLI doesn't
  // benefit from parallelism (subprocess limit), but Promise.all degrades
  // gracefully there too.
  process.stderr.write(`→ counting ${SAMPLES.length} samples in parallel...\n`);
  const t0 = Date.now();
  const rawCounts = await Promise.all(SAMPLES.map((s) => count(s.text)));
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  process.stderr.write(`  done in ${elapsed}s\n\n`);

  const results: SampleResult[] = SAMPLES.map((sample, i) => {
    const localTokens = anthropicLocalCount(sample.text);
    const apiCount = rawCounts[i];
    const claude4Tokens = apiCount - baselineCount + 1; // baseline_local = 1
    const ratio = localTokens > 0 ? claude4Tokens / localTokens : 0;
    return {
      id: sample.id,
      bucket: sample.bucket,
      description: sample.description,
      text_length_chars: sample.text.length,
      local_tokens: localTokens,
      claude4_tokens: claude4Tokens,
      ratio,
      raw_api_input_tokens: apiCount,
    };
  });

  // Print per-sample summary line.
  for (const r of results) {
    process.stderr.write(
      `  ${r.id.padEnd(8)} ${r.bucket.padEnd(6)} ` +
        `local=${String(r.local_tokens).padStart(5)} ` +
        `c4=${String(r.claude4_tokens).padStart(5)} ` +
        `ratio=${r.ratio.toFixed(3)}\n`
    );
  }

  // ── Aggregate stats (overall + per-bucket) ────────────────────────────
  const allRatios = results.map((r) => r.ratio);
  const overall = {
    samples: results.length,
    min_ratio: Math.min(...allRatios),
    median_ratio: median(allRatios),
    p95_ratio: percentile(allRatios, 0.95),
    max_ratio: Math.max(...allRatios),
    mean_ratio: allRatios.reduce((s, r) => s + r, 0) / allRatios.length,
    spread: Math.max(...allRatios) - Math.min(...allRatios),
  };

  // Per-bucket
  const buckets: Record<Bucket, BucketStats> = {} as Record<Bucket, BucketStats>;
  for (const bucket of ["prose", "code", "json", "mixed", "edge"] as Bucket[]) {
    const bucketResults = results.filter((r) => r.bucket === bucket);
    const ratios = bucketResults.map((r) => r.ratio);
    buckets[bucket] = {
      count: bucketResults.length,
      min_ratio: Math.min(...ratios),
      median_ratio: median(ratios),
      p95_ratio: percentile(ratios, 0.95),
      max_ratio: Math.max(...ratios),
      mean_ratio: ratios.reduce((s, r) => s + r, 0) / ratios.length,
    };
  }

  // Non-edge subset stats — useful because edge samples are deliberately
  // pathological and shouldn't drive production multiplier choice.
  const nonEdge = results.filter((r) => r.bucket !== "edge");
  const nonEdgeRatios = nonEdge.map((r) => r.ratio);
  const overallNonEdge = {
    samples: nonEdge.length,
    min_ratio: Math.min(...nonEdgeRatios),
    median_ratio: median(nonEdgeRatios),
    p95_ratio: percentile(nonEdgeRatios, 0.95),
    max_ratio: Math.max(...nonEdgeRatios),
    spread: Math.max(...nonEdgeRatios) - Math.min(...nonEdgeRatios),
  };

  // ── Two scatter datasets for the human reviewer ────────────────────────
  // (1) length × ratio — detects super-linear drift with input size
  // (2) bucket × ratio — detects content-type drift (your tightening #3)
  const lengthScatter = [...results]
    .sort((a, b) => a.local_tokens - b.local_tokens)
    .map((r) => ({ id: r.id, bucket: r.bucket, local_tokens: r.local_tokens, ratio: r.ratio }));
  const bucketScatter = [...results]
    .sort((a, b) => a.bucket.localeCompare(b.bucket) || a.local_tokens - b.local_tokens)
    .map((r) => ({ id: r.id, bucket: r.bucket, local_tokens: r.local_tokens, ratio: r.ratio }));

  // ── Recommendations ───────────────────────────────────────────────────
  // Two factor candidates per your tightening #1:
  //   - p95_factor    = ceil(P95-of-30 * 100) / 100
  //   - max_safe      = ceil(max * 1.05 * 100) / 100  ← more defensible at n=30
  // Same pair computed for non-edge subset, since edge cases are a
  // separate regime that shouldn't drive a single production multiplier.
  const recommendations = {
    p95_overall: Math.ceil(overall.p95_ratio * 100) / 100,
    max_times_1_05_overall: Math.ceil(overall.max_ratio * 1.05 * 100) / 100,
    p95_non_edge: Math.ceil(overallNonEdge.p95_ratio * 100) / 100,
    max_times_1_05_non_edge: Math.ceil(overallNonEdge.max_ratio * 1.05 * 100) / 100,
  };

  const output = {
    meta: {
      date: startedAt,
      tokenizer_under_test: "@anthropic-ai/tokenizer",
      ground_truth_source: tokenizerSource,
      ground_truth_endpoint: apiAvailable
        ? "POST https://api.anthropic.com/v1/messages/count_tokens"
        : "claude -p --output-format json (Pro subscription)",
      model: process.env.LATTICE_BENCH_MODEL ?? "claude-opus-4-7",
      node: process.versions.node,
      platform: `${process.platform}-${process.arch}`,
      sample_count: SAMPLES.length,
      buckets: ["prose", "code", "json", "mixed", "edge"],
      baseline: {
        text: "x",
        local_tokens: 1,
        api_input_tokens: baselineCount,
        note:
          "API baseline includes constant message-framing overhead (~7-13 tokens); " +
          "subtracted from per-sample counts so ratio reflects content tokens only. " +
          "baseline_local taken as 1.",
      },
      counting_path_note:
        "Bench-time counting uses the validated path documented above. " +
        "Runtime budget enforcement in src/util/tokens.ts uses the local " +
        "@anthropic-ai/tokenizer × calibrated multiplier (see recommendation).",
    },
    samples: results,
    statistics: {
      overall,
      overall_non_edge: overallNonEdge,
      by_bucket: buckets,
    },
    scatter: {
      length_x_ratio: lengthScatter,
      bucket_x_ratio: bucketScatter,
    },
    recommendations,
  };

  const dateStr = startedAt.slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", dateStr);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "calibrate-tokenizer.json");
  await fs.writeFile(outPath, JSON.stringify(output, null, 2) + "\n", "utf8");

  process.stderr.write(`\n→ wrote ${path.relative(REPO_ROOT, outPath)}\n\n`);
  process.stderr.write(`overall (n=${overall.samples}):\n`);
  process.stderr.write(
    `  median=${overall.median_ratio.toFixed(3)} ` +
      `P95=${overall.p95_ratio.toFixed(3)} ` +
      `max=${overall.max_ratio.toFixed(3)} ` +
      `spread=${overall.spread.toFixed(3)}\n`
  );
  process.stderr.write(`non-edge (n=${overallNonEdge.samples}):\n`);
  process.stderr.write(
    `  median=${overallNonEdge.median_ratio.toFixed(3)} ` +
      `P95=${overallNonEdge.p95_ratio.toFixed(3)} ` +
      `max=${overallNonEdge.max_ratio.toFixed(3)} ` +
      `spread=${overallNonEdge.spread.toFixed(3)}\n\n`
  );
  process.stderr.write(`recommendation candidates:\n`);
  process.stderr.write(`  P95-of-30        (overall)  = ${recommendations.p95_overall}\n`);
  process.stderr.write(`  max × 1.05       (overall)  = ${recommendations.max_times_1_05_overall}\n`);
  process.stderr.write(`  P95-of-24        (non-edge) = ${recommendations.p95_non_edge}\n`);
  process.stderr.write(`  max × 1.05       (non-edge) = ${recommendations.max_times_1_05_non_edge}\n`);
}

main().catch((err) => {
  console.error("calibrate-tokenizer failed:", err);
  process.exit(1);
});
