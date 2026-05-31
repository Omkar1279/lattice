# Lattice Benchmark Upgrades — Changes & File Registry

This document lists all modifications, newly created utilities, exact file paths, and execution commands implemented during the **Systematic SOTA Benchmark Validation Sweep** (FastAPI and route-guard).

---

## 📁 File Registry & Locations

All paths are relative to the repository root `/Users/asl-user/Documents/personal/claude-plugin-idea/lattice/`:

| Path | Type | Role / Description |
| :--- | :---: | :--- |
| **[`bench/test-leverage.sh`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/test-leverage.sh)** | Shell Script | **Main Benchmark Harness**. Upgraded to support multiple runs ($n$), dynamic seed-based round-robin query paraphrasing, variant shuffling, telemetry collection, and the Aider Repo-Map (Arm E) injection. |
| **[`bench/lib/generate_repomap.py`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/lib/generate_repomap.py)** | Python Script | **Aider Repo-Map Generator**. Replicates Aider's exact prompt-time repository context map by running PageRank over AST calls, imports, inherits, references, and defines. |
| **[`bench/lib/score_answer.py`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/lib/score_answer.py)** | Python Script | **Objective & Narrative Scorer**. Grades model answers against yaml oracle specifications using substring checks or advanced Opus-based narrative judges. |
| **[`bench/lib/summarize_leverage.py`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/lib/summarize_leverage.py)** | Python Script | **Statistical Summarization Engine**. Replaces old student-t averages with mathematically sound median, Interquartile Range (IQR), and non-parametric two-sided **Mann-Whitney U tests**. |
| **[`bench/results/SYSTEMATIC_TEST_PLAN.md`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/results/SYSTEMATIC_TEST_PLAN.md)** | Markdown Doc | **Systematic Test Methodology**. Documents training memorization controls, statistical IQR gates, five comparison arms, and adversarial corpus validation plans. |
| **[`bench/results/SOTA_ARCHITECTURE_DEBATE.md`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/bench/results/walkthrough.md)** | Markdown Doc | **OODA Architectural Self-Debate**. Conducts an analytical critique of TS base regression, RAGAS quality deficits, and competitive push/pull positioning. |
| **[`SKILL.md`](file:///Users/asl-user/Documents/personal/claude-plugin-idea/lattice/SKILL.md)** | Markdown Doc | **Behavioral Retrieval Router**. Specifies prompt-level guidance helping Sonnet choose structured graph navigations over expensive reads. |

---

## 🛠️ Detailed Benchmark Changes

### 1. Harness Upgrades (`bench/test-leverage.sh`)
* **Arm E (Aider Repo-Map) Wiring**:
  * Added support for the `BENCH_ARM` environment variable.
  * When `BENCH_ARM="E"`, the harness automatically runs an initial tree indexing, calls `generate_repomap.py` to extract PageRank ranked symbols, caches the map, and then completely purges the SQLite vault.
  * During the `without` variant runs, the harness prepends the pre-generated Repo-Map text directly to the first user turn prompt and runs vanilla `claude -p` (completely bypassing the Lattice plugin).
* **Paraphrase Confounder Control**: Loads intent paraphrases from `oracle.yaml` dynamically using the loop counter `run_num` and `BENCH_ORDER_SEED` to average out phrasing fixation.
* **Variant Shuffle**: Shuffles the execution order of `with` and `without` per run, eliminating cache-warmup bias.

### 2. Repo-Map Logic (`bench/lib/generate_repomap.py`)
* Extracts the standard PageRank transition weights: `calls=1.0, imports=0.8, inherits=0.9, references=0.5, defines=1.0`.
* Builds a weighted adjacency list from the SQLite `edges` table.
* Executes standard Personalized PageRank power iterations ($10$ rounds, damping $\alpha=0.85$).
* Groups ranked symbols by file path and outputs a formatted tree layout matching Aider's schema.

### 3. Scoring & Math Engines
* **`score_answer.py`**: Resolves `ModuleNotFoundError: No module named 'bench.lib'` inside isolated throwaway clones by registering `__init__.py` packages and resolving paths correctly.
* **`summarize_leverage.py`**: Reads run JSON outputs, groups them, extracts token and cost statistics, runs a two-sided Mann-Whitney U test, and reports medians, IQRs, and statistical significance.

---

## 🚀 Execution & Replication Commands

Ensure you are in the repository root and use the virtual environment python interpreter (`.venv/bin/python`) to run benchmarks.

### 1. Execute Pilot Run (n=1, Pair 2 only)
Runs a fast pilot sweep to verify environment, wiring, and grading:
```bash
LATTICE_HIPPORAG=on BENCH_ARM=E BENCH_FIXTURE_NAME=route-guard BENCH_REPO_DIR=/Users/asl-user/Documents/personal/claude-plugin-idea/route-guard BENCH_RUNS=1 BENCH_PAIRS="2" ./bench/test-leverage.sh
```

### 2. Execute Complete Sweeps (n=5, All Pairs)
Runs the full, rigorous 40-session comparison on the clean post-cutoff `route-guard` fixture:
```bash
LATTICE_HIPPORAG=on BENCH_ARM=E BENCH_FIXTURE_NAME=route-guard BENCH_REPO_DIR=/Users/asl-user/Documents/personal/claude-plugin-idea/route-guard BENCH_RUNS=5 BENCH_PAIRS="1 2 3 4" ./bench/test-leverage.sh
```

### 3. Run Standard Unit Tests
Verifies that all 124 core indexing, semantic retrieval, and graph telemetry unit tests remain green:
```bash
.venv/bin/python -m pytest
```
