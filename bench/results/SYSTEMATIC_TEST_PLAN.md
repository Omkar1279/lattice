# Systematic Test Plan — Validating Lattice for SOTA Claims

**Purpose**: Convert the gates in `SOTA_ARCHITECTURE_DEBATE.md` (Move 1 + §6) into a reproducible, statistically defensible test methodology. Output: a decision matrix that survives peer review.

**Existing foundation**: `bench/test-leverage.sh` already supports `BENCH_REPO_DIR`, `BENCH_RUNS`, public/private fixture toggle, and pairs `p1..p4` for cross-session / corpus-scale / graph-nav / decision-persist. This plan extends — does not replace.

---

## 1. Fixture Matrix (the adversarial set)

Four corpora, one per bucket, each justifying its slot. **Do not substitute "another popular Python repo" for any of these — they share bias direction.**

| Bucket | Fixture | Lang | Files | Why this one |
|---|---|---|---|---|
| **Memorization-contaminated OSS** | `fastapi @ 0.115.0` (already in harness) | Python | ~700 | Heavily in training. Baseline gets memorized-prior lift. If HippoRAG wins *here*, the result is robust. |
| **Clean / post-cutoff OSS** | Pick a repo first-released or substantially-rewritten after 2025-09 (Claude 4.x cutoff). Candidates: a 2026 framework, a recent project from `trending` on GitHub with <500 stars. **Must verify low training presence** — see §1a. | TS or Python | 300–1000 | No memorized priors. Clean differential. |
| **Private / proprietary** | `uniacco-site` (already in harness, `BENCH_REPO_DIR` default) | TS | 695 | Represents real user workload. Already validated as the +24.4% data point. |
| **Sparse-graph stress** | A repo with weak cross-module edges. Candidates: `cobra` (Go), `serde` (Rust subcrate), or a monorepo subtree with internal package boundaries. | Go or Rust | 200–800 | HippoRAG's PPR signal weakens when edges are sparse. Tests whether the +24.4% generalizes outside dense-import shapes. |

### 1a. Verifying "clean / post-cutoff"

Before accepting a candidate, run a 2-prompt sanity probe via `claude -p`:

```
Prompt 1: "Without using any tools, describe the architecture of <repo-name>."
Prompt 2: "Without using any tools, list the top-level modules in <repo-name>."
```

- If Claude produces specific file/symbol names (not generic guesses), the repo is contaminated — reject.
- If Claude refuses or hedges ("I don't have specific information about..."), the repo qualifies.

Record this probe output in `bench/fixtures/<name>/training-probe.txt` so the contamination claim is auditable.

---

## 2. Arm Matrix (what's being compared)

The four-way comparison should be extended to five arms once Move 2 lands:

| Arm | Config | What it tests |
|---|---|---|
| **A. baseline** | no plugin, vanilla Claude Code | Floor / sanity reference |
| **B. lattice-base** | TS Base (current default) | The "are we regressive?" question |
| **C. lattice-hippo** | `LATTICE_HIPPORAG=on` | The proposed new default |
| **D. lattice-contextual** ⚠️ *reference only* | `LATTICE_CONTEXTUAL_CHUNKS=on` | Cache-tax reference + turn-count ceiling. **Not a decision arm** — see note below. |
| **E. aider-repomap** | Aider's repo-map injected at session-start, no other Aider features | The real competitor |

Arm E is the gap in current benchmarking. Implementation: run `aider --show-repo-map > /tmp/aider-map.txt`, prepend the file's contents to the first user turn via a custom `claude -p` system-prompt prefix. This isn't a perfect Aider replication, but it captures the *push-context-at-session-start* mechanism that's their actual moat.

**Arm D is demoted.** Rationale:
- The −27.8% cost-leverage result is directionally settled and mechanically understood (cache bloat from body-appended summaries). Further runs won't change the conclusion.
- Indexing tax (~$1.50 + ~40min Haiku API per fixture) makes full-matrix coverage expensive without buying decision value.
- The SOTA doc explicitly does not invest in Contextual unless cache-compaction is engineered first.
- But: it remains the only data point proving 9-turn dominance is achievable. Keeping a small reference run preserves the turn-count ceiling against which HippoRAG and Aider are judged.

**Arm D scope**: n=2 runs × 4 fixtures × pairs `{p2, p3}` only = **16 total runs**. (p1 cross-session and p4 decision-persist don't exercise Contextual's mechanism; p2 corpus-scale and p3 graph-nav are where it demonstrated 9-turn dominance.) Reported as a *reference column* in §9, not subject to the decision gates in §8.

---

## 3. Sample Size & Statistical Handling

The current harness default `BENCH_RUNS=5` is borderline. LLM agentic runs typically show 25–40% coefficient-of-variation in turn count and cost. With n=5, you can reliably detect effects ≥30% but not the ±15–25% range where Lattice's lift lives.

**Recommendation**:
- **n=10 per (fixture × arm × pair) cell** for the four *decision arms* (A, B, C, E) that gate default-flip and SOTA claims.
- **n=2 per (fixture × pair) cell** for the *reference arm* (D, lattice-contextual), restricted to pairs p2 and p3 only.
- **n=5** acceptable for exploratory / regression-detection runs.

**Run budget**:
- Decision arms (A, B, C, E): 4 fixtures × 4 arms × 4 pairs × 10 = **640 runs**
- Reference arm (D): 4 fixtures × 1 arm × 2 pairs × 2 = **16 runs**
- **Total: 656 runs**. At ~60s/run + ~$0.20/run, that's ~11h wall-clock and ~$135 API budget — plus ~$6 and ~3h of one-time Haiku indexing for Arm D across the 4 fixtures. Run overnight, parallelize across machines if available.

**Reporting**:
- **Report median + IQR**, not mean ± stddev. LLM outputs are heavy-tailed (occasional 50-turn outliers); means lie.
- **Decision rule for "X beats Y"**: median of X is better AND the 25th percentile of X is better than the 75th percentile of Y (i.e., IQRs don't overlap badly). This is a non-parametric "clearly better" gate, not a t-test (which assumes normality LLM costs don't satisfy).
- For tighter inference, run a Mann-Whitney U test; require p < 0.05 with Bonferroni correction across the 16 pair-fixture cells.

---

## 4. Confounder Controls

Each is a real source of false positives we've all seen burn LLM benchmarks.

| Confounder | Control |
|---|---|
| **Model version drift** | Pin to `claude-sonnet-4-6` explicitly via `--model` flag. Record model ID in every result JSON (the existing harness does — verify it continues). |
| **Cache state across runs** | Run an explicit cache-warmup query before measuring, OR flush cache state between arms (currently impossible — Anthropic doesn't expose cache invalidation). Compromise: **interleave arm order** (`A,B,C,D,E,A,B,C,D,E,...`) so cache effects average across arms, rather than batching all `A` runs first. |
| **Query phrasing variance** | The four pair prompts are currently single-string. For each measured cell, use **3 paraphrases** of the same intent (round-robin across the 10 runs). Reduces "model fixated on a specific phrasing" artifact. |
| **Index staleness across runs** | Re-index between runs for arms B/C/D — current harness re-indexes per pair, verify it also re-indexes per run. |
| **Filesystem caching** | Restart the Lattice daemon between arms (already done by `cleanup()`); add explicit `purge` for OS page cache if rigor demands (`sudo purge` on macOS). Probably overkill. |
| **API-side rate fluctuations** | Run each cell's 10 samples spread across at least 2 different hours of the day. Anthropic's inference latency varies by load. |
| **Run-order bias within a cell** | Within the 10 runs for a cell, shuffle the 3 paraphrases pseudo-randomly with a fixed seed (so reruns are reproducible). |

---

## 5. Metrics — Primary, Secondary, Diagnostic

### Primary (decision-gating)

1. **Cost leverage** = `(cost_baseline - cost_arm) / cost_baseline`. Positive = arm wins.
2. **Turn count** — median per (fixture × arm × pair).
3. **Faithfulness** (RAGAS-style, but on a *non-FastAPI* corpus — see §6 below).

### Secondary (sanity / regression watch)

4. **Output token count** — sometimes drops mask poor answer quality; pair with faithfulness to catch.
5. **Cache creation tokens** — Contextual's cache-tax problem; should not regress on other arms.
6. **Wall-clock time** — UX matters; an arm that wins on cost but doubles wall-time is a bad default.

### Diagnostic (for failure analysis)

7. **Tool call count per turn** — surfaces the over-engagement loop signature directly.
8. **Recall hit rate** — fraction of `recall` calls that returned ≥1 chunk the agent then used (`recall_expand`'d or quoted in final answer). Low hit rate = retrieval is noise.
9. **Read-block trigger count** — number of times `pre-tool-use` denied a Read. The proposed moat in action.

---

## 6. The RAGAS Quality Gate — Fix Before Re-Running

Current RAGAS run (faithfulness 0.327, answer_relevance 0.136) is:
- **Synthetic 10-query fixture** of unknown corpus. *Audit it.*
- If the corpus is FastAPI, the LLM-as-judge's priors are contaminating the score — repeat on a clean corpus.
- 10 queries is too small. Expand to 50, drawn from real Claude Code session logs if possible (with PII scrubbing).

**Concrete steps**:
1. Open `bench/fixtures/ragas-queries.yaml` and confirm which corpus the queries refer to.
2. If FastAPI: rebuild a 50-query set against a *clean post-cutoff* corpus (same as fixture bucket 2 above).
3. Re-run RAGAS with `ragas-judge-model` pinned to `claude-opus-4-7` (stronger judge than the model under test — reduces self-preference bias).
4. **Gate**: faithfulness ≥ 0.7 before any SOTA claim. If we can't get there with the current chunking + reranker, that's the blocking architectural finding, not a benchmark detail.

---

## 7. The Full Run Sequence — End-to-End

```
[Week 1] Fixture prep
  Day 1   : pick & verify clean post-cutoff fixture (run §1a probe)
  Day 1   : pick & verify sparse-graph fixture
  Day 2   : land Aider arm wiring (Arm E)
  Day 2   : audit RAGAS fixture; rebuild on clean corpus if contaminated
  Day 3   : land confidence-routing (Move 2 from SOTA doc) behind flag

[Week 2] Pilot run (n=3 per cell, full matrix, find harness bugs)
  Day 4   : run full 4×5×4×3 = 240 runs; sanity-check output JSON shapes
  Day 5   : fix harness issues; check confounders are actually being controlled
            (re-index between runs? arm interleaving correct?)

[Week 3] Decision run (n=10 per cell, gated cells only)
  Day 6-7 : run 4×5×4×10 = 800 runs overnight, two evenings
  Day 8   : compute medians, IQRs, Mann-Whitney p-values; build results matrix
  Day 9   : RAGAS faithfulness on clean corpus (50 queries × 5 arms = 250 judgments)

[Week 4] Decision & write-up
  Day 10  : apply decision rules below; flip defaults if gates pass
  Day 11  : amend SOTA doc with the actual numbers (replace n=1 hedges)
```

---

## 8. Decision Rules — Operationalized

Each rule is a binary gate. If the gate doesn't pass, the corresponding change does not ship.

### Gate G1 — "Flip default to HippoRAG"
**Passes if**: Arm C beats Arm A in median cost leverage on **≥3/4 fixtures**, *including the memorization-contaminated fixture* (FastAPI). Mann-Whitney p < 0.05 on each winning fixture. No fixture shows Arm C losing to Arm A by >10%.

**Action on pass**: flip default in v0.2.
**Action on fail (partial)**: keep flag, sharpen docs (e.g., "lattice excels on dense-import TS frontends; consider for other shapes").
**Action on fail (full)**: don't flip; HippoRAG remains opt-in.

### Gate G2 — "Confidence routing ships"
**Passes if**: with Move 2 enabled, median turn count on Arm B drops ≥15% on ≥3/4 fixtures with no cost regression.

### Gate G3 — "Aider claim is defensible"
**Passes if**: Arm C beats Arm E in median cost leverage on **≥3/4 fixtures**. (If we *lose* to Aider on ≥2 fixtures, the marketing story must change — we are not "the leading local code retrieval"; we are "the local code retrieval with hook-level interception" — narrow the claim.)

### Gate G4 — "SOTA claim is allowed"
**Passes if**: G1 + G2 + G3 all pass, AND faithfulness ≥ 0.7 on clean RAGAS corpus, AND at least one independent reproduction (e.g., another developer runs the suite on their hardware and gets results within 1.5× IQR of ours).

---

## 9. What the Output Looks Like

The deliverable from this plan is a single table written to `bench/results/sota-matrix-<date>.md`:

```
DECISION ARMS (n=10, all 4 pairs)
                            FastAPI    Clean-OSS    uniacco    Sparse
                            (contam.)  (post-2025)  (private)  (Go/Rust)
arm A (baseline)              $0.18       $0.22      $0.26      $0.19
arm B (lattice-base)        -19.3%*     -22.0%*    -19.3%*     -14.0%
arm C (lattice-hippo)        +XX.X%      +XX.X%    +24.4%      +XX.X%
arm E (aider-repomap)        +XX.X%      +XX.X%    +XX.X%      +XX.X%

REFERENCE ARM — turn-count ceiling, not a decision arm
(n=2, pairs p2 + p3 only; cost-leverage gates do NOT apply)
                            FastAPI    Clean-OSS    uniacco    Sparse
arm D (lattice-contextual)  turns:XX   turns:XX   turns:9    turns:XX
                            cost:-XX%  cost:-XX%  cost:-27.8% cost:-XX%

faithfulness (RAGAS):        clean-corpus only:    0.XX  (gate: ≥0.7)
```

The reference arm exists to answer "what's the best turn count achievable on this fixture" so HippoRAG and Aider's turn counts can be judged against a known ceiling. Its cost-leverage column is reported for completeness but is **not** a decision input.

That table is the SOTA claim. Anything we say beyond what's in it is editorial.

---

## 10. What to Cut If Time-Boxed

If full §7 timeline is infeasible, the **minimum defensible cut** is:
- 2 fixtures (memorization-contaminated + private) — drop sparse-graph and clean-post-cutoff
- 3 decision arms (A, B, C) — drop Aider for v0.1, run later. Contextual is already demoted to reference-only and stays at its small footprint (skip entirely in this cut if budget is the constraint).
- n=5 runs (accept wider error bars; report as "preliminary")
- Skip RAGAS rebuild — explicitly note current 0.327 number as known-bad

That cut yields **2 × 3 × 4 × 5 = 120 runs** (~2h, ~$24). Sufficient to make the default-flip decision; insufficient for a SOTA claim. Document the cut explicitly in the results doc so the limitation is visible, not hidden.
