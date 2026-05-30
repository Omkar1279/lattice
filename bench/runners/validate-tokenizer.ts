#!/usr/bin/env tsx
/**
 * docs/benchmarking.md §3 Step 0 — tokenizer validation.
 *
 * Counts each lattice tool schema two ways:
 *   - locally with @anthropic-ai/tokenizer (the package currently in deps)
 *   - via Claude itself (ground truth for Claude 4)
 *
 * Two source modes for the ground-truth count:
 *   - "api"      — POST /v1/messages/count_tokens (free, requires
 *                  ANTHROPIC_API_KEY). Default when the key is set.
 *   - "claude-cli" — shell out to `claude -p --output-format json` and read
 *                  usage.input_tokens. Uses your Claude Code subscription
 *                  (no API key needed) but each call is a real inference
 *                  call so it consumes plan quota.
 *
 * Mode selection:
 *   LATTICE_BENCH_TOKENIZER_SOURCE=api          # explicit API mode
 *   LATTICE_BENCH_TOKENIZER_SOURCE=claude-cli   # explicit CLI mode
 *   (unset) → api if ANTHROPIC_API_KEY is set, else claude-cli, else local-only.
 *
 * If the gap exceeds 5%, the doc requires switching to the API for all bench
 * work. This script writes a JSON to
 * `bench/results/<YYYY-MM-DD>/tokenizer-validation.json` recording both
 * numbers, the gap, and the verdict.
 *
 * Usage:
 *   ANTHROPIC_API_KEY=sk-ant-... npx tsx bench/runners/validate-tokenizer.ts
 *   LATTICE_BENCH_TOKENIZER_SOURCE=claude-cli npx tsx bench/runners/validate-tokenizer.ts
 *   LATTICE_BENCH_MODEL=claude-opus-4-7 ...   # override the pinned model
 *
 * Without either auth path, the script still computes local counts and
 * writes a JSON noting `ground_truth_skipped` — useful for smoke-testing.
 */

import { countTokens as anthropicLocalCount } from "@anthropic-ai/tokenizer";
import { recallTool } from "../../src/tools/recall.js";
import { recallExpandTool } from "../../src/tools/recall_expand.js";
import { writeTool } from "../../src/tools/write.js";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const MODEL = process.env.LATTICE_BENCH_MODEL ?? "claude-opus-4-7";
const API_KEY = process.env.ANTHROPIC_API_KEY;
const SOURCE_OVERRIDE = process.env.LATTICE_BENCH_TOKENIZER_SOURCE;
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

type GroundTruthSource = "api" | "claude-cli" | "skipped";

function pickSource(): GroundTruthSource {
  if (SOURCE_OVERRIDE === "api") return "api";
  if (SOURCE_OVERRIDE === "claude-cli") return "claude-cli";
  if (SOURCE_OVERRIDE && SOURCE_OVERRIDE !== "skipped") {
    throw new Error(
      `LATTICE_BENCH_TOKENIZER_SOURCE must be 'api'|'claude-cli'|'skipped', got '${SOURCE_OVERRIDE}'`
    );
  }
  if (API_KEY) return "api";
  // No API key — try the CLI path. We do NOT auto-detect `claude` here
  // because failing on the first call gives a cleaner error than skipping.
  return "claude-cli";
}

// Tools as they appear on the Anthropic Messages API: { name, description,
// input_schema }. The MCP source uses inputSchema (camelCase) — rename for
// the API. This is exactly the shape Bench 1 will count.
interface ApiTool {
  name: string;
  description: string;
  input_schema: unknown;
}

function toApiTool(t: { name: string; description: string; inputSchema: unknown }): ApiTool {
  return { name: t.name, description: t.description, input_schema: t.inputSchema };
}

const TOOLS: ApiTool[] = [
  toApiTool(recallTool),
  toApiTool(recallExpandTool),
  toApiTool(writeTool),
];

interface PerToolCount {
  name: string;
  json_chars: number;
  local_tokens: number;
  api_tokens: number | null;
  diff_pct_local_vs_api: number | null;
}

interface CountTokensResponse {
  input_tokens: number;
}

async function callCountTokens(body: unknown): Promise<number> {
  if (!API_KEY) throw new Error("ANTHROPIC_API_KEY not set");
  const res = await fetch("https://api.anthropic.com/v1/messages/count_tokens", {
    method: "POST",
    headers: {
      "x-api-key": API_KEY,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`count_tokens API ${res.status}: ${errText}`);
  }
  const data = (await res.json()) as CountTokensResponse;
  return data.input_tokens;
}

/**
 * CLI path: shell out to `claude -p --output-format json` with the prompt on
 * stdin, parse the result, return usage.input_tokens.
 *
 * Why this works as a tokenizer:
 *   `usage.input_tokens` is the count Claude itself reports for the user
 *   message after framing. It uses the same tokenizer the count-tokens API
 *   uses, just behind an inference call. The CLI's system prompt and built-in
 *   tools are billed separately under cache_creation_input_tokens, so they
 *   don't pollute input_tokens.
 *
 * Why we still need a baseline:
 *   The user message gets wrapped in some internal framing (role markers,
 *   chat-template tokens). Comparing (cli_with_content - cli_baseline) to
 *   (local_with_content - local_baseline) cancels that constant out.
 */
interface ClaudeCliUsage {
  input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
}

interface ClaudeCliResult {
  usage: ClaudeCliUsage;
  result?: string;
  is_error?: boolean;
  api_error_status?: unknown;
}

async function callClaudeCli(prompt: string): Promise<ClaudeCliUsage> {
  return new Promise((resolve, reject) => {
    // --system-prompt "" replaces CC's default system prompt (cuts out the
    //   workspace context the model would otherwise see).
    // --append-system-prompt cheap directive so the model emits 1-2 output
    //   tokens instead of trying to do something with our schema dump.
    // --max-budget-usd is a hard floor against runaway pricing if the
    //   environment somehow falls through to a non-subscription billing path.
    // --no-session-persistence keeps the session out of ~/.claude history.
    const child = spawn(
      "claude",
      [
        "-p",
        "--output-format",
        "json",
        "--model",
        MODEL,
        "--system-prompt",
        "",
        "--append-system-prompt",
        "Reply with exactly: ok",
        "--no-session-persistence",
        "--max-budget-usd",
        "0.50",
      ],
      { stdio: ["pipe", "pipe", "pipe"] }
    );

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        return reject(
          new Error(`claude -p exited ${code}\nstderr: ${stderr}\nstdout: ${stdout.slice(0, 500)}`)
        );
      }
      try {
        const parsed = JSON.parse(stdout) as ClaudeCliResult;
        if (parsed.is_error) {
          return reject(new Error(`claude -p reported error: ${JSON.stringify(parsed)}`));
        }
        if (!parsed.usage) {
          return reject(new Error(`claude -p response missing usage field: ${stdout.slice(0, 500)}`));
        }
        resolve(parsed.usage);
      } catch (err) {
        reject(new Error(`failed to parse claude -p JSON: ${err}\noutput: ${stdout.slice(0, 500)}`));
      }
    });

    child.stdin.write(prompt);
    child.stdin.end();
  });
}

/** Signed percentage difference of `a` relative to `b`, one decimal place. */
function pct(a: number, b: number): number {
  if (b === 0) return 0;
  return Math.round(((a - b) / b) * 1000) / 10;
}

async function main() {
  const startedAt = new Date().toISOString();
  const source = pickSource();

  // ── Local counts (always run) ───────────────────────────────────────────
  const perTool: PerToolCount[] = TOOLS.map((t) => {
    const json = JSON.stringify(t);
    return {
      name: t.name,
      json_chars: json.length,
      local_tokens: anthropicLocalCount(json),
      api_tokens: null,
      diff_pct_local_vs_api: null,
    };
  });
  const localTotal = perTool.reduce((s, t) => s + t.local_tokens, 0);

  // ── Ground-truth counts (path depends on source) ───────────────────────
  let groundTruthBlock: Record<string, unknown> = { source };
  let groundTotal: number | null = null;
  let groundError: string | null = null;
  let totalDiffPct: number | null = null;

  if (source === "api") {
    const minimalMessage = [{ role: "user", content: "hi" }];
    let baselineApi: number | null = null;
    let allToolsApi: number | null = null;
    try {
      baselineApi = await callCountTokens({ model: MODEL, messages: minimalMessage });
      for (const tool of perTool) {
        const apiTool = TOOLS.find((t) => t.name === tool.name)!;
        const withOne = await callCountTokens({
          model: MODEL,
          messages: minimalMessage,
          tools: [apiTool],
        });
        tool.api_tokens = withOne - baselineApi;
        tool.diff_pct_local_vs_api = pct(tool.local_tokens, tool.api_tokens);
      }
      allToolsApi = await callCountTokens({
        model: MODEL,
        messages: minimalMessage,
        tools: TOOLS,
      });
      groundTotal = allToolsApi - baselineApi;
      totalDiffPct = pct(localTotal, groundTotal);
    } catch (err) {
      groundError = err instanceof Error ? err.message : String(err);
    }
    groundTruthBlock = {
      source,
      endpoint: "POST https://api.anthropic.com/v1/messages/count_tokens",
      baseline_tokens: baselineApi,
      all_tools_tokens: allToolsApi,
      all_tools_delta_tokens: groundTotal,
      total_diff_pct_local_vs_api: totalDiffPct,
      error: groundError,
    };
  } else if (source === "claude-cli") {
    // CLI mode: count via `claude -p --output-format json`. The TRUE input
    // size is input_tokens + cache_creation + cache_read — Claude Code
    // opportunistically caches the prompt once it crosses ~1K tokens, and
    // input_tokens alone only reports the uncached remainder.
    //
    // We send TWO calls and compare their delta:
    //   baseline_text     = JSON.stringify(writeTool)      // smallest schema
    //   with_content_text = JSON.stringify(TOOLS)          // all three
    // Both prompts are large enough to trigger caching, so CC's constant
    // ~9K-token system overhead cancels in (cli_total_full - cli_total_base).
    // We then check whether that CLI delta matches the local-tokenizer delta.
    const totalUsage = (u: ClaudeCliUsage) =>
      u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens;

    let baselineTotalCli: number | null = null;
    let withContentTotalCli: number | null = null;
    let baselineLocal: number | null = null;
    let withContentLocal: number | null = null;
    try {
      // Baseline: the write tool alone (smallest of the three).
      const writeApi = TOOLS.find((t) => t.name === "lattice.write")!;
      const baselineText = JSON.stringify(writeApi);
      baselineLocal = anthropicLocalCount(baselineText);

      // With-content: all three tools as one JSON array.
      const withContentText = JSON.stringify(TOOLS);
      withContentLocal = anthropicLocalCount(withContentText);

      console.error(`→ baseline call (writeTool, ${baselineLocal} local tokens)`);
      const baselineUsage = await callClaudeCli(baselineText);
      baselineTotalCli = totalUsage(baselineUsage);

      console.error(`→ with-content call (all 3 tools, ${withContentLocal} local tokens)`);
      const withContentUsage = await callClaudeCli(withContentText);
      withContentTotalCli = totalUsage(withContentUsage);

      const cliDelta = withContentTotalCli - baselineTotalCli;
      const localDelta = withContentLocal - baselineLocal;

      // Per-tool: with only 2 calls we can't isolate each tool, but we can
      // attribute the full-set delta proportionally for diagnostic purposes.
      const localSum = perTool.reduce((s, t) => s + t.local_tokens, 0);
      for (const tool of perTool) {
        const proportionalCli = Math.round((tool.local_tokens / localSum) * cliDelta);
        tool.api_tokens = proportionalCli;
        tool.diff_pct_local_vs_api = pct(tool.local_tokens, proportionalCli);
      }

      groundTotal = cliDelta;
      totalDiffPct = pct(localDelta, cliDelta);

      groundTruthBlock = {
        source,
        command: "claude -p --output-format json",
        claude_version: "2.1.142",
        method:
          "delta-of-deltas: send both writeTool alone and all 3 tools, " +
          "subtract CLI totals (input + cache_creation + cache_read) to cancel " +
          "CC's constant system overhead. Compare delta to local-tokenizer delta.",
        baseline: {
          prompt: "JSON.stringify(writeTool)",
          local_tokens: baselineLocal,
          cli_total_tokens: baselineTotalCli,
          cli_breakdown: baselineUsage,
        },
        with_content: {
          prompt: "JSON.stringify([recall, recall_expand, write])",
          local_tokens: withContentLocal,
          cli_total_tokens: withContentTotalCli,
          cli_breakdown: withContentUsage,
        },
        local_delta: localDelta,
        cli_delta: cliDelta,
        total_diff_pct_local_vs_cli: totalDiffPct,
        error: null as string | null,
      };
    } catch (err) {
      groundError = err instanceof Error ? err.message : String(err);
      groundTruthBlock = {
        source,
        command: "claude -p --output-format json",
        claude_version: "2.1.142",
        baseline_total_tokens_cli: baselineTotalCli,
        with_content_total_tokens_cli: withContentTotalCli,
        local_baseline: baselineLocal,
        local_with_content: withContentLocal,
        error: groundError,
      };
    }
  }

  // ── Verdict ────────────────────────────────────────────────────────────
  let verdict: string;
  if (source === "skipped") {
    verdict = "skipped: no ground-truth source available. Set ANTHROPIC_API_KEY or ensure `claude` CLI is logged in, then re-run.";
  } else if (groundError) {
    verdict = `${source}_error: ${groundError}`;
  } else if (totalDiffPct === null || groundTotal === null) {
    verdict = "incomplete: ground-truth counts missing";
  } else if (Math.abs(totalDiffPct) > 5) {
    verdict =
      `local @anthropic-ai/tokenizer differs by ${totalDiffPct}% from ${source} ground-truth ` +
      `(local=${localTotal}, ${source}=${groundTotal}) — SWITCH to ${source} for all bench work.`;
  } else {
    verdict =
      `local @anthropic-ai/tokenizer within 5% of ${source} ground-truth (${totalDiffPct}%) — ` +
      `local tokenizer is acceptable for bench work.`;
  }

  const result = {
    meta: {
      date: startedAt,
      model: MODEL,
      node: process.versions.node,
      platform: `${process.platform}-${process.arch}`,
      tokenizer_source: source,
    },
    local: {
      tokenizer: "@anthropic-ai/tokenizer",
      total_tokens: localTotal,
      per_tool: perTool,
    },
    ground_truth: groundTruthBlock,
    verdict,
  };

  // Write JSON to bench/results/YYYY-MM-DD/tokenizer-validation.json
  const dateStr = startedAt.slice(0, 10);
  const outDir = path.join(REPO_ROOT, "bench", "results", dateStr);
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, "tokenizer-validation.json");
  await fs.writeFile(outPath, JSON.stringify(result, null, 2) + "\n", "utf8");

  console.log(JSON.stringify(result, null, 2));
  console.error(`\n→ wrote ${path.relative(REPO_ROOT, outPath)}`);
  console.error(`→ ${verdict}`);

  if (groundError) process.exit(2);
}

main().catch((err) => {
  console.error("validate-tokenizer failed:", err);
  process.exit(1);
});
