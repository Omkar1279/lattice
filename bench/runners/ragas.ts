#!/usr/bin/env tsx
/**
 * RAGAS triad evaluation — local, LLM-free implementation.
 *
 * Measures three axes of retrieval quality without requiring an LLM judge:
 *   - Faithfulness: Jaccard similarity between answer words and context words
 *   - Answer Relevance: Jaccard similarity between query words and answer words
 *   - Context Precision: fraction of retrieved chunks in expected_chunks
 *
 * Plus token-cost tracking per query (sum of countTokens for retrieved chunks).
 *
 * Output: bench/results/<YYYY-MM-DD>/ragas.json
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import yaml from "js-yaml";
import { countTokens } from "../../src/util/tokens.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

// ── Types ───────────────────────────────────────────────────────────────────

/**
 * Retrieved chunk in a RAGAS fixture: an ID plus a body text.
 *
 * Prior fixture (pre-fix) used `retrieved_chunks: string[]` where the strings
 * were chunk *bodies*, while `expected_chunks` held *IDs*. The runner then
 * checked `retrievedSet ∩ expectedSet` — always empty (bodies ≠ IDs),
 * so context_precision was 0 on every query. The new shape lets the runner
 * use id-set membership for precision and body text for the Jaccard scores.
 */
export interface RetrievedChunk {
  id: string;
  body: string;
}

export interface RagasQuery {
  query: string;
  expected_chunks: string[];
  retrieved_chunks: Array<string | RetrievedChunk>; // string accepted for back-compat (body-only)
  answer?: string;
}

export interface RagasScore {
  query: string;
  faithfulness: number;
  answer_relevance: number;
  context_precision: number;
  token_cost: number;
}

export interface RagasResult {
  scores: RagasScore[];
  aggregates: {
    mean_faithfulness: number;
    mean_answer_relevance: number;
    mean_context_precision: number;
    total_token_cost: number;
  };
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function tokenize(text: string): Set<string> {
  return new Set(
    text.toLowerCase().replace(/[^a-z0-9_]/g, " ").split(/\s+/).filter(Boolean)
  );
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let intersection = 0;
  for (const w of a) if (b.has(w)) intersection++;
  const union = a.size + b.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

// ── Core evaluation ─────────────────────────────────────────────────────────

function asRetrieved(c: string | RetrievedChunk): RetrievedChunk {
  return typeof c === "string" ? { id: c, body: c } : c;
}

export function evaluateRagas(queries: RagasQuery[]): RagasResult {
  const scores: RagasScore[] = queries.map((q) => {
    const retrieved = q.retrieved_chunks.map(asRetrieved);
    const answerText = q.answer ?? "";
    const contextText = retrieved.map((r) => r.body).join(" ");

    const queryWords = tokenize(q.query);
    const answerWords = tokenize(answerText);
    const contextWords = tokenize(contextText);

    // Faithfulness: how much of the answer is grounded in context (body text).
    const faithfulness = answerWords.size > 0
      ? jaccard(answerWords, contextWords)
      : 0;

    // Answer Relevance: how relevant the answer is to the question.
    const answer_relevance = answerWords.size > 0
      ? jaccard(queryWords, answerWords)
      : 0;

    // Context Precision: fraction of retrieved IDs in expected set.
    // (Pre-fix this compared bodies to IDs and was always 0.)
    const expectedSet = new Set(q.expected_chunks);
    const relevant = retrieved.filter((c) => expectedSet.has(c.id));
    const context_precision = retrieved.length > 0
      ? relevant.length / retrieved.length
      : 0;

    // Token cost: sum of countTokens for all retrieved bodies.
    const token_cost = retrieved.reduce(
      (sum, chunk) => sum + countTokens(chunk.body),
      0
    );

    return { query: q.query, faithfulness, answer_relevance, context_precision, token_cost };
  });

  const n = scores.length || 1;
  const aggregates = {
    mean_faithfulness: scores.reduce((s, r) => s + r.faithfulness, 0) / n,
    mean_answer_relevance: scores.reduce((s, r) => s + r.answer_relevance, 0) / n,
    mean_context_precision: scores.reduce((s, r) => s + r.context_precision, 0) / n,
    total_token_cost: scores.reduce((s, r) => s + r.token_cost, 0),
  };

  return { scores, aggregates };
}

// ── CLI runner ──────────────────────────────────────────────────────────────

async function main() {
  const fixtureFile = path.join(REPO_ROOT, "bench", "fixtures", "ragas-queries.yaml");
  const raw = await fs.readFile(fixtureFile, "utf-8");
  const data = yaml.load(raw) as { queries: RagasQuery[] };

  const result = evaluateRagas(data.queries);

  // Write output
  const today = new Date().toISOString().slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", today);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "ragas.json");
  await fs.writeFile(outPath, JSON.stringify(result, null, 2) + "\n");

  console.log(`\n── RAGAS Triad Results ──`);
  console.log(`  Faithfulness (mean):       ${result.aggregates.mean_faithfulness.toFixed(3)}`);
  console.log(`  Answer Relevance (mean):   ${result.aggregates.mean_answer_relevance.toFixed(3)}`);
  console.log(`  Context Precision (mean):  ${result.aggregates.mean_context_precision.toFixed(3)}`);
  console.log(`  Total Token Cost:          ${result.aggregates.total_token_cost}`);
  console.log(`\nWritten to: ${outPath}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
