# Lattice Python Benchmarks

> **Status**: green (all 5 core functional benchmark runners ported and successfully verified).
> This file is the official operational benchmark suite summary. Raw results are stored under `bench/results/`.

**Last updated**: 2026-05-30
**Lattice version**: `0.1.0` (Python port)
**Model**: `claude-opus-4-7`
**Machine**: macOS arm64, Python 3.12.11

---

## Benchmark Inventory & Headline Metrics

Every benchmark has been cleanly ported and executed on the Python codebase. R@K results are evaluated against the fastapi fixture (`fastapi@0.115.0`).

| Bench | Metric | Result | vs Serena (TS) |
|---|---|---|---|
| **1. Tool-Schema Overhead** | input tokens / turn | **630** | Serena: **8,177** (Python: 13x savings) |
| | tokens saved per 50-turn session | **377,350** | Serena: **344,900** |
| **2. Retrieval Sanity Check** | overall R@5 / R@10 / MRR | **0.75 / 0.80 / 0.67** | n/a |
| | symbol R@5 / MRR | **0.90 / 0.90** (PASS ≥ 0.9) | n/a |
| | behavioural R@5 / R@10 | **0.60 / 0.70** | n/a |
| **4. Freshness Scenarios** | scenarios passing | **10/10** ✅ (100% pass) | n/a |
| **RAGAS Triad Evaluation** | mean context_precision | **0.558** | Matches TS identically |
| | mean faithfulness | **0.327** | Matches TS identically |
| | mean answer_relevance | **0.136** | Matches TS identically |

---

## Bench 1 — Tool-Schema Token Overhead

**Headline**: `lattice-python` exposes only **630 tokens** of MCP tool schema per turn — **13× less than Serena**'s 8,177 tokens across its 28-tool surface. Across a standard 50-turn session, this saves **377,350 input tokens** on tool schemas alone!

### Results Summary

| Server | Tools | Total tokens | vs lattice (per turn) | Source |
|---|---:|---:|---:|---|
| **lattice (python)** | **3** | **630** | — | live MCP stdio spawn |
| Serena (TS) | 28 | 8,177 | +7,547 | live MCP stdio spawn |
| Anthropic Memory Tool | 1 | 426 | +196 | hand-authored from docs |
| mem0 | — | — | skipped | missing env: `OPENAI_API_KEY` |
| Cipher | — | — | skipped | always skipped (unverifiable install) |

Raw run: [`bench/results/tool-overhead.json`](bench/results/tool-overhead.json).
Reproduce: `uv run python bench/runners/tool-overhead.py`

---

## Bench 2 — Retrieval Sanity Check

**Headline**: Indexes the FastAPI `0.115.0` fixture, generating **2,028 chunks and 5,115 symbols in 7.4 seconds**. The retrieval cascade achieved an overall R@5 of **0.75** and successfully satisfied the symbol R@5 gate of **0.90** (≥ 0.9 target).

### Query Metrics

| Category | n | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|
| **symbol** | 10 | **0.90** | **0.90** | **0.90** |
| **behavioural** | 10 | **0.60** | **0.70** | **0.43** |
| **overall** | **20** | **0.75** | **0.80** | **0.67** |

Raw run: [`bench/results/retrieval-sanity.json`](bench/results/retrieval-sanity.json).
Reproduce: `uv run python bench/runners/retrieval-sanity.py`

---

## Bench 4 — Freshness Scenarios

**Headline**: All **10/10 scenarios pass** successfully. The decay curves, F1→F2→F3 supersedes cascades, conflict overrides, path scoping, and pinned exclusions are fully compliant with the specification.

| # | Scenario | Result |
|---:|---|---|
| 1 | decay ordering (T-1d > T-7d > T-30d) | ✓ PASS |
| 2 | F2 supersedes F1 → F1 omitted | ✓ PASS |
| 3 | supersedes chain F1→F2→F3 → only F3 returned | ✓ PASS |
| 4 | conflicting facts, no supersedes → newer wins by recency | ✓ PASS |
| 5 | high source-weight stale beats low source-weight fresh | ✓ PASS |
| 6 | tag-based weighting (no tag scoring modifiers) | ✓ PASS |
| 7 | revoke chain: F1 stays suppressed when F3 supersedes F2 | ✓ PASS |
| 8 | cross-path supersedes: path scope does not unhide superseded chunks | ✓ PASS |
| 9 | decay floor: T-1y fact retrievable when no fresher equivalent exists | ✓ PASS |
| 10 | pinned fact at T-90d outranks fresh unpinned | ✓ PASS |

Raw run: [`bench/results/freshness.json`](bench/results/freshness.json).
Reproduce: `uv run python bench/runners/freshness.py`

---

## Tokenizer Validation & Calibration

### Step 0 Tokenizer Agreement
Comparing the local `tiktoken` counter against the official Anthropic Tokenizer API (`/v1/messages/count_tokens`) ground truth using `.anthropic-key`:

| Source | Tokens for schema delta (write -> all 3 tools) |
|---|---:|
| `tiktoken` (local) | **764** |
| `anthropic-api` (ground truth) | **628** |
| **Discrepancy** | **+21.7%** (conservative ceiling safety margin) |

Raw run: [`bench/results/tokenizer-validation.json`](bench/results/tokenizer-validation.json).
Reproduce: `uv run python bench/runners/validate-tokenizer.py`

### 30-Sample Calibration
Evaluates local counts against `anthropic-api` across 30 real-content samples:

*   **overall (n=30)**: median=0.898, P95=1.018, max=1.571, spread=1.071
*   **non-edge (n=24)**: median=0.898, P95=0.969, max=1.018, spread=0.284

**Calibrated Multiplier**: The calibrated multiplier candidates for P95 non-edge sit at `0.97` and max × 1.05 non-edge sits at `1.07`. Since `lattice-python` utilizes the original port's `1.78` multiplier, it remains highly conservative and absolutely safe, never underestimating token budgets inside the context window.

All ground-truth measurements now run over the free, zero-cost official Anthropic Tokenizer API via `.anthropic-key`, ensuring zero subscription quota is used during benchmark runs.

Raw run: [`bench/results/calibrate-tokenizer.json`](bench/results/calibrate-tokenizer.json).
Reproduce: `uv run python bench/runners/calibrate-tokenizer.py`

---

## RAGAS Triad offline Evaluation

Calculates faithfulness, answer relevance, and context precision on a synthetic 10-query fixture:

*   **mean faithfulness**: 0.327
*   **mean answer_relevance**: 0.136
*   **mean context_precision**: 0.558

Raw run: [`bench/results/ragas.json`](bench/results/ragas.json).
Reproduce: `uv run python bench/runners/ragas.py`
