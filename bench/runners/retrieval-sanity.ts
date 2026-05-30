#!/usr/bin/env tsx
/**
 * Retrieval sanity check (per user review of v0 queries.yaml):
 *
 * Runs the 20 verified queries through full-cascade `lattice.recall`
 * against an indexed fastapi@40e33e49 fixture and reports R@5 / R@10 / MRR.
 * If symbol R@5 < 0.9 here, either the queries are too LLM-clever for
 * lattice's BM25, or the indexer has a bug. Either way we want to know
 * before scaling to 50.
 *
 * NOT the ablation run. Single condition, full cascade. Pure sanity.
 *
 * Methodology notes:
 *   - Path-level matching: a result counts as a hit if the chunk's path
 *     matches any required-or-acceptable path for the query. Whole-file
 *     chunking means symbol-level grading would only matter for the real
 *     ablation; for sanity it's enough that retrieval surfaces the right
 *     file in top-K.
 *   - Cascade returns up to ~25 fused chunks (15 BM25 + 10 semantic). We
 *     set budget_tokens artificially high so the packing step doesn't
 *     truncate before top-10 is captured.
 *   - The query strings in queries.yaml are natural-language sentences,
 *     so the cascade's symbol-pass early-exit (single-identifier queries
 *     only) won't fire — every query exercises BM25 + freshness rerank.
 *     This is the realistic developer-asks-naturally test path.
 *
 * Output: bench/results/<YYYY-MM-DD>/retrieval-sanity.json
 */

import * as fs from "node:fs/promises";
import * as fsSync from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { fileURLToPath } from "node:url";
import yaml from "js-yaml";
import { openVault } from "../../src/storage/vault.js";
import { runCascade } from "../../src/retrieval/cascade.js";
import { reindexRepo } from "../../src/indexer/indexer.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const QUERIES_PATH = path.join(REPO_ROOT, "bench", "fixtures", "queries.yaml");
const FIXTURE_REPO_PATH = "/tmp/lattice-bench-fastapi-0.115.0";
const FIXTURE_SHA = "40e33e492dbf4af6172997f4e3238a32e56cbe26";

// If .anthropic-key exists in the repo root and ANTHROPIC_API_KEY isn't
// already set, load it. This lets local devs run with the contextual stage
// active without exporting a key globally. The key file is gitignored.
function maybeLoadAnthropicKey(): boolean {
  if (process.env.ANTHROPIC_API_KEY) return true;
  const keyPath = path.join(REPO_ROOT, ".anthropic-key");
  try {
    const raw = fsSync.readFileSync(keyPath, "utf8");
    const key = raw.trim();
    if (key.length > 0) {
      process.env.ANTHROPIC_API_KEY = key;
      return true;
    }
  } catch {
    // file missing — that's fine, contextual stage will no-op gracefully
  }
  return false;
}

// Scope contextualization to the fastapi/ source subtree only. The bench
// retrieves with path_scope = ${FIXTURE_REPO_PATH}/fastapi/, so paying for
// LLM calls on tests/ + docs_src/ chunks is pure waste — they'd never be
// retrieved. This drops the API bill from ~2k chunks to ~150.
function setContextualPathScope(): void {
  if (process.env.LATTICE_CONTEXTUAL_PATH_SCOPE) return;
  process.env.LATTICE_CONTEXTUAL_PATH_SCOPE =
    path.join(FIXTURE_REPO_PATH, "fastapi") + "/";
}

interface RequiredHit {
  path: string;
  symbol: string;
  note?: string;
}

interface Query {
  id: string;
  category: string;
  query: string;
  required: RequiredHit[];
  acceptable?: RequiredHit[];
}

interface QueriesFile {
  fixture: string;
  queries: Query[];
}

interface QueryResult {
  id: string;
  category: string;
  query: string;
  required_paths: string[];
  acceptable_paths: string[];
  hit_at_5: boolean;
  hit_at_10: boolean;
  first_hit_rank: number | null; // 1-indexed; null if no hit anywhere
  top_5: { rank: number; path: string | null; heading: string }[];
}

async function verifyFixture(): Promise<void> {
  try {
    await fs.access(FIXTURE_REPO_PATH);
  } catch {
    throw new Error(
      `Fixture not found at ${FIXTURE_REPO_PATH}. ` +
        `Re-clone with: git clone --depth 1 --branch 0.115.0 https://github.com/tiangolo/fastapi ${FIXTURE_REPO_PATH}`
    );
  }
  // Best-effort SHA verification — if .git was shallow-cloned the rev-parse
  // still works. We check via spawn rather than a git library to keep deps low.
  const { exec } = await import("node:child_process");
  const { promisify } = await import("node:util");
  const execP = promisify(exec);
  const { stdout } = await execP("git rev-parse HEAD", { cwd: FIXTURE_REPO_PATH });
  const head = stdout.trim();
  if (head !== FIXTURE_SHA) {
    throw new Error(
      `Fixture SHA mismatch. expected=${FIXTURE_SHA} got=${head}. ` +
        `Re-clone the fixture cleanly.`
    );
  }
}

async function main() {
  const startedAt = new Date().toISOString();

  // ── Pre-flight ────────────────────────────────────────────────────────
  process.stderr.write(`→ verifying fixture at ${FIXTURE_SHA}...\n`);
  await verifyFixture();

  // Auto-load .anthropic-key from repo root if present, then scope
  // contextualization to fastapi/ source only. These two together keep
  // the contextual-chunks LLM bill under ~$0.05 even on a 2k-chunk repo.
  const haveKey = maybeLoadAnthropicKey();
  setContextualPathScope();
  const contextualOn = process.env.LATTICE_CONTEXTUAL_CHUNKS === "on";
  process.stderr.write(
    `→ contextual=${contextualOn ? "on" : "off"}` +
      `  key=${haveKey ? "present" : "missing"}` +
      `  scope=${process.env.LATTICE_CONTEXTUAL_PATH_SCOPE ?? "(unset)"}\n`
  );
  if (contextualOn && !haveKey) {
    process.stderr.write(
      `  ⚠ LATTICE_CONTEXTUAL_CHUNKS=on but no key — stage will no-op.\n`
    );
  }

  process.stderr.write(`→ loading queries.yaml...\n`);
  const queriesText = await fs.readFile(QUERIES_PATH, "utf8");
  const queriesDoc = yaml.load(queriesText) as QueriesFile;
  const queries = queriesDoc.queries;
  process.stderr.write(`  ${queries.length} queries loaded\n`);

  // ── Fresh vault ───────────────────────────────────────────────────────
  const vaultDir = await fs.mkdtemp(path.join(os.tmpdir(), "lattice-sanity-"));
  process.stderr.write(`→ opening fresh vault at ${vaultDir}...\n`);
  const vault = await openVault(vaultDir);

  // ── Index fastapi ─────────────────────────────────────────────────────
  process.stderr.write(`→ indexing ${FIXTURE_REPO_PATH}...\n`);
  const indexT0 = Date.now();
  await reindexRepo(vault, FIXTURE_REPO_PATH);
  const indexElapsedSec = ((Date.now() - indexT0) / 1000).toFixed(1);
  const chunkCount = (
    vault.db.prepare(`SELECT COUNT(*) as n FROM chunks WHERE source = 'code_index'`).get() as {
      n: number;
    }
  ).n;
  const symbolCount = (
    vault.db.prepare(`SELECT COUNT(*) as n FROM symbols`).get() as { n: number }
  ).n;
  process.stderr.write(
    `  indexed ${chunkCount} chunks, ${symbolCount} symbols in ${indexElapsedSec}s\n\n`
  );

  // Run each query through full cascade. We pass an absolute path_scope
  // pointing at the fastapi/ source subdirectory so retrieval doesn't drown
  // in fastapi's enormous tests/ + docs_src/ trees (combined ~95% of the
  // 1.3K-chunk corpus). Real lattice users would typically scope to the
  // module they're working on; this matches that pattern.
  const SOURCE_SCOPE = path.join(FIXTURE_REPO_PATH, "fastapi") + "/";
  const results: QueryResult[] = [];
  for (const q of queries) {
    const cascadeResults = await runCascade(vault, {
      query: q.query,
      // Set artificially high so packToBudget doesn't truncate before
      // top-10. With ~25 fused chunks @ ~200 tokens each, this is plenty.
      budget_tokens: 100000,
      kind: "all",
      path_scope: SOURCE_SCOPE,
    });

    // Look up paths for ranked chunks. Cascade returns them already in
    // RRF + freshness order, so the array index IS the rank (0-based).
    const ranked = await Promise.all(
      cascadeResults.map(async (r) => {
        const chunk = await vault.getChunk(r.id);
        if (!chunk?.path) return { rank: 0, path: null, heading: r.heading };
        const relativePath = path.relative(FIXTURE_REPO_PATH, chunk.path);
        return { rank: 0, path: relativePath, heading: r.heading };
      })
    );
    ranked.forEach((r, i) => (r.rank = i + 1));

    const requiredPaths = q.required.map((r) => r.path);
    const acceptablePaths = (q.acceptable ?? []).map((a) => a.path);
    // For "hit": ANY required-or-acceptable path showing up in top-K. The
    // ablation will be stricter (per docs/benchmarking.md §4: required ALL
    // must appear); the sanity check uses the looser definition because
    // most of our queries have a single required entry anyway.
    const validPaths = new Set([...requiredPaths, ...acceptablePaths]);
    const hitRank = ranked.findIndex((r) => r.path !== null && validPaths.has(r.path));
    const firstHitRank = hitRank >= 0 ? hitRank + 1 : null;

    results.push({
      id: q.id,
      category: q.category,
      query: q.query,
      required_paths: requiredPaths,
      acceptable_paths: acceptablePaths,
      hit_at_5: firstHitRank !== null && firstHitRank <= 5,
      hit_at_10: firstHitRank !== null && firstHitRank <= 10,
      first_hit_rank: firstHitRank,
      top_5: ranked.slice(0, 5),
    });

    const status = firstHitRank === null ? "✗ MISS" : firstHitRank <= 5 ? "✓" : "△";
    process.stderr.write(
      `  ${q.id} [${q.category.padEnd(11)}] ` +
        `rank=${firstHitRank ?? "-"}  ${status}  "${q.query.slice(0, 60)}"\n`
    );
  }

  vault.close();

  // ── Aggregate stats ──────────────────────────────────────────────────
  const overall = {
    n: results.length,
    r_at_5: results.filter((r) => r.hit_at_5).length / results.length,
    r_at_10: results.filter((r) => r.hit_at_10).length / results.length,
    mrr:
      results.reduce(
        (sum, r) => sum + (r.first_hit_rank !== null ? 1 / r.first_hit_rank : 0),
        0
      ) / results.length,
  };

  const byCategory: Record<string, typeof overall> = {};
  for (const cat of ["symbol", "behavioural"] as const) {
    const subset = results.filter((r) => r.category === cat);
    if (subset.length === 0) continue;
    byCategory[cat] = {
      n: subset.length,
      r_at_5: subset.filter((r) => r.hit_at_5).length / subset.length,
      r_at_10: subset.filter((r) => r.hit_at_10).length / subset.length,
      mrr:
        subset.reduce(
          (sum, r) => sum + (r.first_hit_rank !== null ? 1 / r.first_hit_rank : 0),
          0
        ) / subset.length,
    };
  }

  // ── Verdict ───────────────────────────────────────────────────────────
  const symbolR5 = byCategory.symbol?.r_at_5 ?? 0;
  const verdict =
    symbolR5 >= 0.9
      ? `PASS: symbol R@5 = ${symbolR5.toFixed(2)} ≥ 0.9. Queries + indexer are healthy enough to scale to 50.`
      : `FAIL: symbol R@5 = ${symbolR5.toFixed(2)} < 0.9. Either queries are too clever or the indexer has a bug. Investigate before drafting the remaining 30.`;

  const output = {
    meta: {
      date: startedAt,
      fixture: queriesDoc.fixture,
      fixture_path: FIXTURE_REPO_PATH,
      vault_path: vaultDir,
      chunks_indexed: chunkCount,
      symbols_indexed: symbolCount,
      index_elapsed_sec: parseFloat(indexElapsedSec),
      node: process.versions.node,
      platform: `${process.platform}-${process.arch}`,
      methodology:
        "single condition (full cascade), path-level R@K. Hit = any required " +
        "or acceptable path appears in top-K of fused-and-reranked results.",
    },
    overall,
    by_category: byCategory,
    verdict,
    per_query: results,
  };

  const dateStr = startedAt.slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", dateStr);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "retrieval-sanity.json");
  await fs.writeFile(outPath, JSON.stringify(output, null, 2) + "\n", "utf8");

  process.stderr.write(`\n→ wrote ${path.relative(REPO_ROOT, outPath)}\n\n`);
  process.stderr.write(`overall (n=${overall.n}):\n`);
  process.stderr.write(
    `  R@5  = ${overall.r_at_5.toFixed(2)}  ` +
      `R@10 = ${overall.r_at_10.toFixed(2)}  ` +
      `MRR  = ${overall.mrr.toFixed(2)}\n`
  );
  for (const [cat, stats] of Object.entries(byCategory)) {
    process.stderr.write(
      `${cat.padEnd(11)} (n=${stats.n}):  ` +
        `R@5  = ${stats.r_at_5.toFixed(2)}  ` +
        `R@10 = ${stats.r_at_10.toFixed(2)}  ` +
        `MRR  = ${stats.mrr.toFixed(2)}\n`
    );
  }
  process.stderr.write(`\n${verdict}\n`);
}

main().catch((err) => {
  console.error("retrieval-sanity failed:", err);
  process.exit(1);
});
