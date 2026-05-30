#!/usr/bin/env tsx
/**
 * retrieval-smoke (CodeRAG-Bench-shaped, NOT the published dataset).
 *
 * This is a 5-case synthetic smoke test that mimics CodeRAG-Bench's two
 * dominant task shapes (HumanEval-style docstring→impl and RepoBench-style
 * partial-file→completion) using hand-written fixtures in
 * `bench/fixtures/coderag/`. It is NOT the published CodeRAG-Bench corpus
 * (HumanEval / MBPP / RepoBench / SWE-bench-Lite, ~9K total problems).
 *
 * What this catches:
 *   - cascade end-to-end smoke (does recall return anything?)
 *   - chunk-id heading matching against retrieval results
 *   - regressions in the symbol/BM25/RRF/rerank pipeline on toy inputs
 *
 * What this does NOT prove:
 *   - performance on real code-search distributions
 *   - generalisation to unseen repos
 *   - anything resembling the published CodeRAG-Bench leaderboard numbers
 *
 * Headline numbers from this runner are NOT comparable to published
 * CodeRAG-Bench results and should not be cited as such. Honest framing:
 * "synthetic 5-case retrieval smoke, CodeRAG-Bench-shaped."
 *
 * Output: bench/results/<YYYY-MM-DD>/retrieval-smoke.json
 *
 * To run against the real CodeRAG-Bench dataset, wire in
 * https://github.com/code-rag-bench/code-rag-bench (TODO: not implemented).
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as os from "node:os";
import { fileURLToPath } from "node:url";
import { openVault } from "../../src/storage/vault.js";
import { runCascade } from "../../src/retrieval/cascade.js";
import { countTokens } from "../../src/util/tokens.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const FIXTURE_DIR = path.join(REPO_ROOT, "bench", "fixtures", "coderag");

// ── Types ───────────────────────────────────────────────────────────────────

interface TestCase {
  id: string;
  type: "humaneval" | "repobench";
  query: string;
  expected_file: string;
  expected_chunk_id: string;
}

interface CaseResult {
  id: string;
  type: string;
  query: string;
  expected_chunk_id: string;
  hit_in_top5: boolean;
  reciprocal_rank: number; // 1/rank if found, 0 otherwise
  top_5_ids: string[];
}

interface BenchResult {
  cases: CaseResult[];
  aggregates: {
    recall_at_5: number;
    mrr: number;
    by_type: Record<string, { recall_at_5: number; mrr: number; count: number }>;
  };
}

// ── Test cases ──────────────────────────────────────────────────────────────

const TEST_CASES: TestCase[] = [
  {
    id: "he-001",
    type: "humaneval",
    query: "Calculate the fibonacci sequence up to n terms using memoization",
    expected_file: "fibonacci.ts",
    expected_chunk_id: "coderag/fibonacci.ts",
  },
  {
    id: "he-002",
    type: "humaneval",
    query: "Parse a CSV string into an array of objects using the header row as keys",
    expected_file: "csv-parser.ts",
    expected_chunk_id: "coderag/csv-parser.ts",
  },
  {
    id: "he-003",
    type: "humaneval",
    query: "Implement a least-recently-used cache with a maximum capacity",
    expected_file: "lru-cache.ts",
    expected_chunk_id: "coderag/lru-cache.ts",
  },
  {
    id: "rb-001",
    type: "repobench",
    query: "import { EventEmitter } from 'events';\nimport { Logger } from './logger';\n\nexport class ConnectionPool {\n  private pool: Map<string, Connection> = new Map();\n  private logger: Logger;\n",
    expected_file: "connection-pool.ts",
    expected_chunk_id: "coderag/connection-pool.ts",
  },
  {
    id: "rb-002",
    type: "repobench",
    query: "import * as fs from 'node:fs';\nimport * as path from 'node:path';\n\nexport interface WatcherOptions {\n  recursive?: boolean;\n  filter?: (p: string) => boolean;\n}\n",
    expected_file: "file-watcher.ts",
    expected_chunk_id: "coderag/file-watcher.ts",
  },
];

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  // Set up a temp vault for indexing
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "lattice-coderag-"));
  const vaultDir = path.join(tmpDir, ".lattice");
  await fs.mkdir(path.join(vaultDir, "notes"), { recursive: true });

  const vault = await openVault(vaultDir);

  // Index all fixture files
  const fixtureFiles = await fs.readdir(FIXTURE_DIR);
  for (const file of fixtureFiles.filter((f) => f.endsWith(".ts"))) {
    const content = await fs.readFile(path.join(FIXTURE_DIR, file), "utf-8");
    const chunkId = `coderag/${file}`;
    await vault.writeNote({
      heading: file,
      body: content,
      tags: ["coderag", file.replace(".ts", "")],
      source: "auto_capture",
    });
    // Also insert directly for chunk ID matching
    vault.db.prepare(
      `INSERT OR REPLACE INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at)
       VALUES (?, ?, ?, 'code_index', ?, '["coderag"]', datetime('now'), datetime('now'), datetime('now'))`
    ).run(chunkId, file, content, `bench/fixtures/coderag/${file}`);
    // Update FTS
    vault.db.prepare(
      `INSERT INTO chunks_fts (rowid, heading, body) VALUES ((SELECT rowid FROM chunks WHERE id = ?), ?, ?)`
    ).run(chunkId, file, content);
  }

  // Run each test case
  const results: CaseResult[] = [];

  for (const tc of TEST_CASES) {
    const cascadeResults = await runCascade(vault, {
      query: tc.query,
      budget_tokens: 8000,
      kind: "all",
    });

    const top5 = cascadeResults.slice(0, 5);
    const top5Ids = top5.map((r) => r.id);

    // Check if expected chunk appears in top 5 (path-based matching)
    const hitIndex = cascadeResults.findIndex(
      (r) => r.id === tc.expected_chunk_id ||
             r.heading === tc.expected_file ||
             (r.id && r.id.includes(tc.expected_file.replace(".ts", "")))
    );

    const hit_in_top5 = hitIndex >= 0 && hitIndex < 5;
    const reciprocal_rank = hitIndex >= 0 ? 1 / (hitIndex + 1) : 0;

    results.push({
      id: tc.id,
      type: tc.type,
      query: tc.query.slice(0, 80) + (tc.query.length > 80 ? "…" : ""),
      expected_chunk_id: tc.expected_chunk_id,
      hit_in_top5,
      reciprocal_rank,
      top_5_ids: top5Ids,
    });
  }

  // Compute aggregates
  const recall_at_5 = results.filter((r) => r.hit_in_top5).length / results.length;
  const mrr = results.reduce((s, r) => s + r.reciprocal_rank, 0) / results.length;

  const byType: Record<string, { recall_at_5: number; mrr: number; count: number }> = {};
  for (const type of ["humaneval", "repobench"] as const) {
    const subset = results.filter((r) => r.type === type);
    byType[type] = {
      recall_at_5: subset.filter((r) => r.hit_in_top5).length / (subset.length || 1),
      mrr: subset.reduce((s, r) => s + r.reciprocal_rank, 0) / (subset.length || 1),
      count: subset.length,
    };
  }

  const benchResult: BenchResult = {
    cases: results,
    aggregates: { recall_at_5, mrr, by_type: byType },
  };

  // Write output
  const today = new Date().toISOString().slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", today);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "retrieval-smoke.json");
  await fs.writeFile(outPath, JSON.stringify(
    { ...benchResult, dataset: "synthetic-5-case (NOT published CodeRAG-Bench)" },
    null, 2) + "\n");

  console.log(`\n── Retrieval Smoke (CodeRAG-Bench-shaped, synthetic 5-case) ──`);
  console.log(`  NOTE: synthetic local fixtures, not the published dataset.`);
  console.log(`  Recall@5:  ${recall_at_5.toFixed(3)}`);
  console.log(`  MRR:       ${mrr.toFixed(3)}`);
  console.log(`  HumanEval-shaped: R@5=${byType.humaneval.recall_at_5.toFixed(3)} MRR=${byType.humaneval.mrr.toFixed(3)}`);
  console.log(`  RepoBench-shaped: R@5=${byType.repobench.recall_at_5.toFixed(3)} MRR=${byType.repobench.mrr.toFixed(3)}`);
  console.log(`\nWritten to: ${outPath}`);

  vault.close();
  await fs.rm(tmpDir, { recursive: true, force: true });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
