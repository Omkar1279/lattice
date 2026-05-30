#!/usr/bin/env tsx
/**
 * CodeRAG-Bench: HumanEval retrieval subset.
 *
 * Uses the REAL HumanEval dataset (164 problems from openai/human-eval) as
 * both corpus and query set:
 *   - Corpus: each problem's prompt + canonical_solution, written to a temp
 *     .py file and indexed via reindexRepo (exercises the full tree-sitter
 *     + cAST chunking + symbol-extraction + RRF cascade).
 *   - Queries: the natural-language docstring summary (first paragraph,
 *     doctests stripped, function signature stripped) so the retriever has
 *     to bridge docstring → implementation without the function name.
 *   - Ground truth: rank of the chunk whose heading matches entry_point.
 *
 * Metrics: Recall@1, Recall@5, Recall@10, MRR over all 164 queries.
 *
 * HONEST FRAMING — what this DOES and DOES NOT measure:
 *   ✓ This IS real published data (openai/human-eval JSONL).
 *   ✓ This IS a 164-query / 164-chunk retrieval evaluation.
 *   ✗ This is NOT the full CodeRAG-Bench benchmark setup, which uses a
 *     multi-million-chunk background corpus (programming docs, Stack
 *     Overflow, GitHub) layered ON TOP of the task solutions. Our small
 *     self-corpus is materially easier than CodeRAG-Bench's published
 *     configuration; our numbers will be OPTIMISTIC vs their leaderboard.
 *     To wire the full setup, layer in the DocPrompting + StarCoder
 *     background corpora — left as v0.5 follow-up.
 *
 * Output: bench/results/<YYYY-MM-DD>/coderag-humaneval.json
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as os from "node:os";
import { fileURLToPath } from "node:url";
import { openVault } from "../../src/storage/vault.js";
import { reindexRepo } from "../../src/indexer/indexer.js";
import { runCascade } from "../../src/retrieval/cascade.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const HUMANEVAL_PATH = path.join(
  REPO_ROOT,
  "bench",
  "fixtures",
  "coderag-humaneval",
  "HumanEval.jsonl"
);

interface HumanEvalProblem {
  task_id: string;
  prompt: string;
  entry_point: string;
  canonical_solution: string;
  test: string;
}

interface CaseResult {
  task_id: string;
  entry_point: string;
  query: string;
  rank: number; // 1-indexed; -1 if not found in top 20
  hit_at_1: boolean;
  hit_at_5: boolean;
  hit_at_10: boolean;
  reciprocal_rank: number;
}

interface BenchResult {
  dataset: string;
  caveat: string;
  config: {
    embeddings: string;
    contextual_chunks: string;
    reranker: string;
    colbert: string;
  };
  cases: CaseResult[];
  aggregates: {
    recall_at_1: number;
    recall_at_5: number;
    recall_at_10: number;
    mrr: number;
    n: number;
  };
}

/**
 * Extract the natural-language part of a HumanEval docstring.
 * Strips the function signature, the triple quotes, and the doctest examples
 * (anything starting with >>>). Keeps the prose summary the developer would
 * read first.
 */
function extractDocstringSummary(prompt: string): string {
  const tripleQuoteMatch = prompt.match(/"""([\s\S]*?)"""/);
  if (!tripleQuoteMatch) {
    // No docstring — fall back to a trimmed signature line.
    return prompt.replace(/[\s\S]*def\s+\w+\s*\([^)]*\)\s*->?\s*[^:]*:\s*/, "")
      .trim()
      .split("\n")[0] || prompt.slice(0, 200);
  }
  const docBody = tripleQuoteMatch[1];
  const lines = docBody.split("\n");
  const proseLines: string[] = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith(">>>")) break; // stop at first doctest
    if (trimmed.startsWith("Examples:") || trimmed.startsWith("For example")) break;
    proseLines.push(trimmed);
  }
  return proseLines.join(" ").slice(0, 500);
}

async function loadProblems(): Promise<HumanEvalProblem[]> {
  const raw = await fs.readFile(HUMANEVAL_PATH, "utf-8");
  return raw
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as HumanEvalProblem);
}

async function main() {
  const problems = await loadProblems();
  console.error(`→ loaded ${problems.length} HumanEval problems`);

  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "lattice-humaneval-"));
  const corpusDir = path.join(tmpDir, "corpus");
  await fs.mkdir(corpusDir, { recursive: true });

  // Write each problem as a .py file so reindexRepo exercises the full
  // tree-sitter + cAST + symbol pipeline.
  for (const p of problems) {
    const filePath = path.join(corpusDir, `${p.entry_point}.py`);
    await fs.writeFile(filePath, p.prompt + p.canonical_solution, "utf-8");
  }
  console.error(`→ wrote ${problems.length} corpus files to ${corpusDir}`);

  const vaultDir = path.join(tmpDir, ".lattice");
  await fs.mkdir(path.join(vaultDir, "notes"), { recursive: true });
  const vault = await openVault(vaultDir);

  const indexStart = Date.now();
  await reindexRepo(vault, corpusDir);
  const indexMs = Date.now() - indexStart;
  const chunkCount = vault.db.prepare(`SELECT COUNT(*) as n FROM chunks WHERE source = 'code_index'`).get() as { n: number };
  const symbolCount = vault.db.prepare(`SELECT COUNT(*) as n FROM symbols`).get() as { n: number };
  console.error(`→ indexed ${chunkCount.n} chunks, ${symbolCount.n} symbols in ${(indexMs / 1000).toFixed(1)}s`);

  // Run each query
  const cases: CaseResult[] = [];
  let processed = 0;
  for (const p of problems) {
    const query = extractDocstringSummary(p.prompt);
    if (!query) {
      cases.push({
        task_id: p.task_id, entry_point: p.entry_point, query: "",
        rank: -1, hit_at_1: false, hit_at_5: false, hit_at_10: false, reciprocal_rank: 0,
      });
      continue;
    }
    const out = await runCascade(vault, {
      query,
      budget_tokens: 8000,
      kind: "auto",
    });
    // Hit detection: top-K must contain a chunk whose chunk-id (or heading)
    // matches the entry_point file. reindexRepo's chunk-id format is
    // sha256(file:line) sliced — so we search by chunk heading and source path.
    let rank = -1;
    for (let i = 0; i < Math.min(out.length, 20); i++) {
      const c = out[i];
      // Heading from cAST chunking is "<fileName>" for single-chunk files
      // or "<fileName>:<startLine>-<endLine>" for multi-chunk. Either way
      // the entry_point.py prefix is in the heading.
      if (c.heading === `${p.entry_point}.py` || c.heading.startsWith(`${p.entry_point}.py:`)) {
        rank = i + 1;
        break;
      }
    }
    const hit_at_1 = rank === 1;
    const hit_at_5 = rank >= 1 && rank <= 5;
    const hit_at_10 = rank >= 1 && rank <= 10;
    const reciprocal_rank = rank >= 1 ? 1 / rank : 0;
    cases.push({
      task_id: p.task_id, entry_point: p.entry_point, query,
      rank, hit_at_1, hit_at_5, hit_at_10, reciprocal_rank,
    });
    processed++;
    if (processed % 20 === 0) console.error(`  …${processed}/${problems.length} queries done`);
  }

  const n = cases.length;
  const aggregates = {
    recall_at_1: cases.filter((c) => c.hit_at_1).length / n,
    recall_at_5: cases.filter((c) => c.hit_at_5).length / n,
    recall_at_10: cases.filter((c) => c.hit_at_10).length / n,
    mrr: cases.reduce((s, c) => s + c.reciprocal_rank, 0) / n,
    n,
  };

  const result: BenchResult = {
    dataset: "openai/human-eval (164 problems)",
    caveat:
      "HumanEval-only retrieval over a 164-chunk self-corpus. NOT the full " +
      "CodeRAG-Bench setup (which adds a multi-million-chunk background " +
      "corpus). Numbers here are optimistic vs the published leaderboard.",
    config: {
      embeddings: process.env.LATTICE_EMBEDDINGS ?? "off",
      contextual_chunks: process.env.LATTICE_CONTEXTUAL_CHUNKS ?? "off",
      reranker: process.env.LATTICE_RERANKER ?? "off",
      colbert: process.env.LATTICE_COLBERT ?? "off",
    },
    cases,
    aggregates,
  };

  const today = new Date().toISOString().slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", today);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "coderag-humaneval.json");
  await fs.writeFile(outPath, JSON.stringify(result, null, 2) + "\n");

  console.error(`\n── CodeRAG-Bench (HumanEval retrieval, n=${n}) ──`);
  console.error(`  config: embeddings=${result.config.embeddings} contextual=${result.config.contextual_chunks} reranker=${result.config.reranker}`);
  console.error(`  Recall@1:  ${aggregates.recall_at_1.toFixed(3)}`);
  console.error(`  Recall@5:  ${aggregates.recall_at_5.toFixed(3)}`);
  console.error(`  Recall@10: ${aggregates.recall_at_10.toFixed(3)}`);
  console.error(`  MRR:       ${aggregates.mrr.toFixed(3)}`);
  console.error(`\nWritten to: ${outPath}`);

  vault.close();
  await fs.rm(tmpDir, { recursive: true, force: true });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
