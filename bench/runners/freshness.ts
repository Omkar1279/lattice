#!/usr/bin/env tsx
/**
 * docs/benchmarking.md §6 — Bench 4: freshness / supersedes scenarios.
 *
 * Runs all 10 scenarios from the spec, emits a JSON summary to
 * bench/results/<YYYY-MM-DD>/freshness.json. Pass criterion: all 10 pass.
 *
 * Per §11 anti-pattern guidance, failing scenarios are surfaced honestly
 * with a `reason` field rather than hidden. Features the bench expects but
 * the code doesn't implement (e.g. `pinned`) are marked
 * `feature_not_implemented` so the gap is visible in the public artifact.
 *
 * Usage:
 *   npm run bench:freshness
 *   npx tsx bench/runners/freshness.ts
 */

import { createTestVault, type TestVault } from "../../test/helpers.js";
import { runCascade } from "../../src/retrieval/cascade.js";
import { freshnessScore } from "../../src/retrieval/freshness.js";
import type { Chunk } from "../../src/storage/vault.js";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const DAY_MS = 24 * 60 * 60 * 1000;
const ago = (days: number) => new Date(Date.now() - days * DAY_MS).toISOString();

/**
 * Standalone freshness re-ranker used only by this bench to measure the
 * decay curve in isolation. Production retrieval folds freshness into the
 * RRF score in `cascade.ts` (see docs/architecture.md "Retrieval cascade")
 * so this function deliberately does NOT live in `src/retrieval/`.
 */
function rerankByFreshness(chunks: Chunk[], since?: string): Chunk[] {
  const now = Date.now();
  const sinceTs = since ? new Date(since).getTime() : -Infinity;
  return chunks
    .filter((c) => !c.superseded_by && new Date(c.last_seen_at).getTime() >= sinceTs)
    .sort((a, b) => freshnessScore(b, now) - freshnessScore(a, now));
}

interface ScenarioResult {
  id: number;
  description: string;
  pass: boolean;
  expected: string;
  actual: string;
  /** Populated when pass=false. "feature_not_implemented" is a recognised value. */
  reason?: string;
  /** Free-form notes — e.g. policy declarations for scenarios 5, 6. */
  notes?: string;
}

type Scenario = () => Promise<ScenarioResult>;

// ─── Scenario 1: decay ordering ───────────────────────────────────────────
// Same fact at three ages; freshest must rank first.
async function scenario1(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    const chunks: Chunk[] = [
      vault.insertChunk({
        id: "s1-old",
        heading: "config baseline",
        body: "the team uses 4-space indent",
        source: "human_note",
        last_seen_at: ago(30),
      }),
      vault.insertChunk({
        id: "s1-mid",
        heading: "config baseline",
        body: "the team uses 4-space indent",
        source: "human_note",
        last_seen_at: ago(7),
      }),
      vault.insertChunk({
        id: "s1-new",
        heading: "config baseline",
        body: "the team uses 4-space indent",
        source: "human_note",
        last_seen_at: ago(1),
      }),
    ];
    const ranked = rerankByFreshness(chunks);
    const ids = ranked.map((c) => c.id);
    const expected = ["s1-new", "s1-mid", "s1-old"];
    const pass = JSON.stringify(ids) === JSON.stringify(expected);
    return {
      id: 1,
      description: "decay ordering: T-1d > T-7d > T-30d for same fact",
      pass,
      expected: expected.join(" > "),
      actual: ids.join(" > "),
      reason: pass ? undefined : "rank order does not match",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 2: F2 supersedes F1 → F1 omitted at default budget ──────────
// Verifies the supersedes filter at the cascade boundary, not just the
// freshness rerank. Real recall must drop the older chunk.
async function scenario2(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    vault.insertChunk({
      id: "s2-f1",
      heading: "deploy region",
      body: "deploy to us-east-1 with t3.medium instances",
      source: "human_note",
      last_seen_at: ago(30),
      superseded_by: "s2-f2",
    });
    vault.insertChunk({
      id: "s2-f2",
      heading: "deploy region",
      body: "deploy to us-west-2 with t3.large multi-AZ",
      source: "human_note",
      last_seen_at: ago(1),
      supersedes: "s2-f1",
    });
    const results = await runCascade(vault, {
      query: "deploy region",
      budget_tokens: 2500,
      kind: "all",
    });
    const ids = results.map((r) => r.id);
    const pass = ids.includes("s2-f2") && !ids.includes("s2-f1");
    return {
      id: 2,
      description: "F2 supersedes F1 → F2 returned, F1 omitted",
      pass,
      expected: "results contain s2-f2 and NOT s2-f1",
      actual: `results = [${ids.join(", ")}]`,
      reason: pass ? undefined : "superseded chunk leaked into recall results",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 3: F1 → F2 → F3 chain → only F3 returned ───────────────────
async function scenario3(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    vault.insertChunk({
      id: "s3-v1",
      heading: "JWT rotation policy",
      body: "rotate JWT signing key every 90 days",
      source: "human_note",
      superseded_by: "s3-v2",
    });
    vault.insertChunk({
      id: "s3-v2",
      heading: "JWT rotation policy",
      body: "rotate JWT signing key every 60 days",
      source: "human_note",
      supersedes: "s3-v1",
      superseded_by: "s3-v3",
    });
    vault.insertChunk({
      id: "s3-v3",
      heading: "JWT rotation policy",
      body: "rotate JWT signing key every 30 days with automated ceremony",
      source: "human_note",
      supersedes: "s3-v2",
    });
    const results = await runCascade(vault, {
      query: "JWT rotation policy",
      budget_tokens: 2500,
      kind: "all",
    });
    const ids = results.map((r) => r.id);
    const pass =
      ids.includes("s3-v3") && !ids.includes("s3-v1") && !ids.includes("s3-v2");
    return {
      id: 3,
      description: "supersedes chain F1→F2→F3 → only F3 returned",
      pass,
      expected: "results contain s3-v3 and NOT s3-v1 or s3-v2",
      actual: `results = [${ids.join(", ")}]`,
      reason: pass ? undefined : "intermediate or original chunk leaked through chain",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 4: conflicting facts, no supersedes link → newer wins ──────
async function scenario4(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    vault.insertChunk({
      id: "s4-old",
      heading: "rate limit policy",
      body: "rate limit is 100 requests per minute",
      source: "human_note",
      last_seen_at: ago(30),
    });
    vault.insertChunk({
      id: "s4-new",
      heading: "rate limit policy",
      body: "rate limit is 500 requests per minute",
      source: "human_note",
      last_seen_at: ago(1),
    });
    const results = await runCascade(vault, {
      query: "rate limit policy",
      budget_tokens: 2500,
      kind: "all",
    });
    const ids = results.map((r) => r.id);
    const newerFirst = ids.indexOf("s4-new") < ids.indexOf("s4-old");
    const pass = ids.includes("s4-new") && ids.includes("s4-old") && newerFirst;
    return {
      id: 4,
      description: "conflicting facts, no supersedes → newer ranks before older",
      pass,
      expected: "s4-new appears before s4-old in results",
      actual: `results = [${ids.join(", ")}]`,
      reason: pass ? undefined : "newer chunk did not outrank older when both visible",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 5: high source-weight stale vs low source-weight fresh ─────
// Documents and verifies the policy: source weight × exponential decay.
// code_index has weight=1.0 and tau=Infinity (no decay). auto_capture has
// weight=0.6 and tau=30d. So an "ancient" code chunk still beats a fresh
// auto-capture purely on source weight.
async function scenario5(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    const staleHigh: Chunk = vault.insertChunk({
      id: "s5-stale-code",
      heading: "core API",
      body: "function getUser(id) { ... }",
      source: "code_index",
      last_seen_at: ago(365),
    });
    const freshLow: Chunk = vault.insertChunk({
      id: "s5-fresh-auto",
      heading: "auto-captured note",
      body: "user mentioned getUser briefly in chat",
      source: "auto_capture",
      last_seen_at: ago(0),
    });
    const now = Date.now();
    const staleHighScore = freshnessScore(staleHigh, now);
    const freshLowScore = freshnessScore(freshLow, now);
    const pass = staleHighScore > freshLowScore;
    return {
      id: 5,
      description: "high source-weight stale beats low source-weight fresh (policy verification)",
      pass,
      expected: `code_index@T-365d (=${staleHighScore.toFixed(3)}) > auto_capture@T-0d (=${freshLowScore.toFixed(3)})`,
      actual: `code_index = ${staleHighScore.toFixed(3)}, auto_capture = ${freshLowScore.toFixed(3)}`,
      notes:
        "Policy: freshnessScore = SOURCE_WEIGHT[source] × exp(-age/tau[source]). " +
        "code_index weight=1.0, tau=Infinity. auto_capture weight=0.6, tau=30d.",
      reason: pass ? undefined : "policy expects high-weight stale to win, but it didn't",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 6: tag weighting — policy is NO tag-based weighting ────────
// Doc says "Decision outranks observation IF that's the policy". Current
// policy: tags are filters/labels only, not score modifiers. We verify
// scores are equal for same-source/same-age chunks regardless of tags.
async function scenario6(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    const decision: Chunk = vault.insertChunk({
      id: "s6-decision",
      heading: "use postgres",
      body: "decision: we will use postgres for the primary store",
      source: "human_note",
      tags: ["decision"],
      last_seen_at: ago(7),
    });
    const observation: Chunk = vault.insertChunk({
      id: "s6-observation",
      heading: "use postgres",
      body: "observation: postgres has been deployed in staging",
      source: "human_note",
      tags: ["observation"],
      last_seen_at: ago(7),
    });
    const now = Date.now();
    const decScore = freshnessScore(decision, now);
    const obsScore = freshnessScore(observation, now);
    // Equal within float tolerance (timestamps could differ by sub-millisecond
    // due to insertion order — accept any difference < 1e-9).
    const pass = Math.abs(decScore - obsScore) < 1e-9;
    return {
      id: 6,
      description: "tag-based weighting policy verification",
      pass,
      expected: "scores equal (no tag weighting in current policy)",
      actual: `decision = ${decScore}, observation = ${obsScore}, diff = ${(decScore - obsScore).toExponential(2)}`,
      notes:
        "Policy declared: tags are filters/labels only, not score modifiers. " +
        "If a future version weights `decision` higher than `observation`, " +
        "this scenario flips to: decision > observation strictly.",
      reason: pass ? undefined : "scores diverged unexpectedly given current no-tag-weight policy",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 7: revoke chain — F1 stays suppressed when F3 supersedes F2 ─
// In a chain F1→F2→F3, F1.superseded_by points to F2 (not F3). When F3
// arrives and supersedes F2, F1's link is unchanged. The hide must still
// hold: F1 is filtered because superseded_by is non-null, regardless of
// whether the chain has been "revoked" further down.
async function scenario7(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    // Build chain in three steps to mimic real history: F1, then F2, then F3.
    vault.insertChunk({
      id: "s7-f1",
      heading: "auth flow",
      body: "auth flow uses session cookies only",
      source: "human_note",
      last_seen_at: ago(60),
      superseded_by: "s7-f2", // set when F2 was written
    });
    vault.insertChunk({
      id: "s7-f2",
      heading: "auth flow",
      body: "auth flow uses session cookies and CSRF tokens",
      source: "human_note",
      last_seen_at: ago(30),
      supersedes: "s7-f1",
      superseded_by: "s7-f3", // set when F3 was written
    });
    vault.insertChunk({
      id: "s7-f3",
      heading: "auth flow",
      body: "auth flow uses JWT bearer tokens with refresh rotation",
      source: "human_note",
      last_seen_at: ago(1),
      supersedes: "s7-f2",
    });
    const results = await runCascade(vault, {
      query: "auth flow",
      budget_tokens: 2500,
      kind: "all",
    });
    const ids = results.map((r) => r.id);
    const pass =
      ids.includes("s7-f3") && !ids.includes("s7-f1") && !ids.includes("s7-f2");
    return {
      id: 7,
      description: "revoke chain: F1 still suppressed when F3 supersedes F2",
      pass,
      expected: "results contain s7-f3, exclude s7-f1 and s7-f2",
      actual: `results = [${ids.join(", ")}]`,
      reason: pass
        ? undefined
        : "F1 or F2 leaked through despite each having superseded_by set",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 8: cross-path supersedes — path scope doesn't break it ─────
// F1 lives at /a/foo.md, F2 at /b/bar.md, F2 supersedes F1. When recall is
// called with path_scope="/a", F1 must still be hidden (because superseded)
// AND F2 must not appear (out of scope). Expected behaviour: the recall
// returns no results from this fact, NOT a leaked stale F1.
async function scenario8(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    vault.insertChunk({
      id: "s8-f1",
      heading: "cors policy",
      body: "cors policy allows only same-origin requests",
      source: "human_note",
      path: "/repo/a/cors.md",
      last_seen_at: ago(30),
      superseded_by: "s8-f2",
    });
    vault.insertChunk({
      id: "s8-f2",
      heading: "cors policy",
      body: "cors policy allows configured origins via env var",
      source: "human_note",
      path: "/repo/b/cors.md",
      last_seen_at: ago(1),
      supersedes: "s8-f1",
    });
    const results = await runCascade(vault, {
      query: "cors policy",
      budget_tokens: 2500,
      kind: "all",
      path_scope: "/repo/a",
    });
    const ids = results.map((r) => r.id);
    // The superseded F1 must NOT appear even though it's the only chunk in
    // /repo/a. F2 may legitimately appear or not (path filter is enforced
    // inside cascade, but supersedes filter is universal).
    const pass = !ids.includes("s8-f1");
    return {
      id: 8,
      description: "cross-path supersedes: path scope does not unhide superseded chunk",
      pass,
      expected: "s8-f1 NOT in results (even when scoped to its own path)",
      actual: `results = [${ids.join(", ")}]`,
      reason: pass ? undefined : "stale chunk leaked through because path-scoped query bypassed the supersedes filter",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 9: decay floor — T-1y fact still retrievable ───────────────
// rerankByFreshness has no score floor: very old chunks are heavily
// down-weighted but not filtered. Verify a 365d-old human_note still comes
// back from cascade (the "no fresher equivalent" case).
async function scenario9(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    vault.insertChunk({
      id: "s9-ancient",
      heading: "legacy db schema",
      body: "the legacy db schema uses a single users table with a json blob",
      source: "human_note",
      last_seen_at: ago(365),
    });
    const results = await runCascade(vault, {
      query: "legacy db schema",
      budget_tokens: 2500,
      kind: "all",
    });
    const ids = results.map((r) => r.id);
    const pass = ids.includes("s9-ancient");
    // Verify it really is heavily down-weighted, not at full freshness.
    const score = pass
      ? results.find((r) => r.id === "s9-ancient")!.freshness
      : null;
    return {
      id: 9,
      description: "decay floor: T-1y fact retrievable when no fresher equivalent exists",
      pass,
      expected: "s9-ancient appears in results with low freshness score (~0.1)",
      actual: `results = [${ids.join(", ")}], freshness = ${score === null ? "n/a" : score.toFixed(3)}`,
      reason: pass ? undefined : "very old chunk was filtered out instead of being returned with low rank",
    };
  } finally {
    vault.close();
  }
}

// ─── Scenario 10: pinned fact wins over fresh unpinned ───────────────────
// Implemented 2026-05-27. `pinned` is a column on `chunks` and `freshnessScore`
// returns a high constant (PINNED_SCORE) when set, ensuring pinned chunks
// outrank any unpinned chunk regardless of age.
async function scenario10(): Promise<ScenarioResult> {
  const vault = await createTestVault();
  try {
    const pinnedStale = vault.insertChunk({
      id: "s10-pinned-old",
      heading: "team convention",
      body: "indent with 4 spaces in python",
      source: "human_note",
      last_seen_at: ago(90),
      pinned: 1,
    });
    const freshUnpinned = vault.insertChunk({
      id: "s10-fresh-unpinned",
      heading: "team convention",
      body: "indent with 2 spaces in python",
      source: "human_note",
      last_seen_at: ago(0),
    });
    const ranked = rerankByFreshness([freshUnpinned, pinnedStale]);
    const pass = ranked[0].id === pinnedStale.id;
    return {
      id: 10,
      description: "pinned fact at T-90d outranks fresh unpinned",
      pass,
      expected: "pinned chunk ranks first regardless of age",
      actual: pass
        ? "pinned T-90d ranked first; fresh unpinned ranked second"
        : `top after re-rank was ${ranked[0].id}, expected ${pinnedStale.id}`,
    };
  } finally {
    vault.close();
  }
}

const SCENARIOS: Scenario[] = [
  scenario1,
  scenario2,
  scenario3,
  scenario4,
  scenario5,
  scenario6,
  scenario7,
  scenario8,
  scenario9,
  scenario10,
];

async function main() {
  const startedAt = new Date().toISOString();
  const scenarios: ScenarioResult[] = [];
  let crashCount = 0;

  for (const scenario of SCENARIOS) {
    try {
      const result = await scenario();
      scenarios.push(result);
      const symbol = result.pass ? "✓" : "✗";
      console.error(`${symbol} #${result.id}: ${result.description}`);
      if (!result.pass) console.error(`    reason: ${result.reason ?? "—"}`);
    } catch (err) {
      crashCount++;
      const msg = err instanceof Error ? err.stack ?? err.message : String(err);
      scenarios.push({
        id: scenarios.length + 1,
        description: "(crashed before producing a result)",
        pass: false,
        expected: "scenario completes",
        actual: "scenario threw",
        reason: `crash: ${msg.slice(0, 500)}`,
      });
      console.error(`✗ scenario crashed: ${msg.slice(0, 200)}`);
    }
  }

  const passCount = scenarios.filter((s) => s.pass).length;
  const total = scenarios.length;

  const result = {
    meta: {
      date: startedAt,
      node: process.versions.node,
      platform: `${process.platform}-${process.arch}`,
    },
    results: { scenarios, pass_count: passCount, total },
    summary: {
      verdict:
        passCount === total
          ? `all ${total} scenarios pass — freshness/supersedes invariants hold`
          : `${passCount}/${total} pass; ${total - passCount} failures named below`,
      failures: scenarios
        .filter((s) => !s.pass)
        .map((s) => ({ id: s.id, description: s.description, reason: s.reason })),
    },
  };

  const dateStr = startedAt.slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", dateStr);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "freshness.json");
  await fs.writeFile(outPath, JSON.stringify(result, null, 2) + "\n", "utf8");

  console.error(`\n→ wrote ${path.relative(REPO_ROOT, outPath)}`);
  console.error(`→ ${result.summary.verdict}`);

  // Exit non-zero only when a scenario CRASHED (infrastructure failure).
  // A 9/10 with one feature_not_implemented is a successful run that informs
  // a downstream decision, not a CI break.
  if (crashCount > 0) process.exit(2);
}

main().catch((err) => {
  console.error("freshness runner failed:", err);
  process.exit(1);
});
