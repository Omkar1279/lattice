# SOTA Architecture Debate — Can Lattice Compete in the Token-Reducing Game?

**Framework**: OODA (Observe → Orient → Decide → Act), with self-debate.
**Date**: 2026-05-31
**Evidence base**: `BENCH.md`, `OODA_ANALYSIS.md`, `FOUR_WAY_COMPARISON.md` — single fixture (`uniacco-site`, 695 TS files, ~7k edges). All cost-leverage numbers below inherit that n=1 limit and are *directional, not declarative*.

---

## 0. The Frame

The user's question: *what architectural decisions make Lattice toe-to-toe SOTA against the token-reducing competitive set?*

Three honesty constraints, ordered by load-bearing weight:

1. **Lattice ships regressive by default.** The TS Base configuration loses to no-plugin Claude by **−19.3%** cost leverage on the one fixture we've measured. This is not "room for improvement" — it is a product that, in its default state, makes the host agent *worse*. Every architectural decision below must answer: *does this fix the default, or is it a side quest?*
2. **The RAGAS triad is louder than the token-savings headline.** `faithfulness=0.327, answer_relevance=0.136` are not footnotes. If retrieved chunks don't faithfully ground answers, the agent learns to distrust `recall` and re-greps — collapsing the token-savings thesis. Quantity without quality is a hollow win.
3. **n=1 corpus invalidates SOTA claims.** All cost-leverage percentages come from one TypeScript repo (`uniacco-site`, private). The retrieval-sanity R@K numbers come from a *different* fixture (FastAPI 0.115.0). We have enough evidence to *flip defaults* and *prioritize roadmap*; we do **not** have enough to declare a winner. Any SOTA claim is gated behind **adversarially-chosen** N≥3 corpus validation — see §6 for the selection criterion. Naively picking "TS, Python, Go" is not sufficient if all three are repos Claude has memorized.

### 2a. A note on fixture-bias risk (memorization)

Claude has seen popular OSS repos in training (FastAPI, Django, React, Next.js, etc.). On those repos:
- The **no-plugin baseline gets unfair lift** — Claude answers from memorized priors without needing tool calls. Any plugin looks worse by comparison, because the plugin can only help when the model doesn't already know the answer.
- **R@K retrieval metrics are *not* affected** by this — R@K measures whether the retriever surfaces the gold chunk, independent of what Claude does with it. The FastAPI fixture is fine for retrieval-sanity.
- **RAGAS-style judged metrics *are* potentially affected** — the LLM-as-judge can be biased by its own priors about the corpus. Faithfulness/relevance scores on FastAPI need to be sanity-checked.
- **The leverage benchmark numbers in this repo are *not* affected by FastAPI memorization** — they ran on `uniacco-site`, a private repo. But uniacco-site is itself n=1 and one specific shape (TS frontend, dense imports, single-developer codebase). Generalization to other shapes is unproven.

---

## 1. The Real Competitive Set (not just Serena)

Serena (28 tools, 8,177 token schema) is a strawman. The genuine competition:

| Competitor | What they do | Lattice's relative position |
|---|---|---|
| **Aider repo-map** | tree-sitter + PageRank on imports/calls, injected as prompt-time text map. No daemon, no MCP. | **Closest architectural twin.** They got there first with the same primitives. Discriminator: hook *interception* (deny redundant reads), auto-capture of bash output, MCP-native (not prompt-injected). |
| **Cursor indexer** | Cloud-side semantic index, deep IDE integration. | We don't compete at scale or IDE polish. We compete on *local-only* + *Claude Code hook depth*. |
| **Sourcegraph Cody** | Enterprise graph indexing (zoekt), code intelligence. | Same as Cursor — enterprise scope. We are the single-developer, single-repo, single-machine play. |
| **Anthropic Memory Tool** | 1 tool, 426 token schema, server-side opaque storage. | Even leaner schema than our 630. Opaque — no repo graph. *Positioning*: companion, not replacement. `lattice export-to-memory-tool` should exist. |
| **mem0 / Letta / MemGPT** | LLM-mediated long-term memory abstractions. | Different shape — conversational memory, not codebase retrieval. Adjacent, not rival. |
| **Microsoft GraphRAG / LightRAG** | Graph-of-summaries built from documents via LLM extraction. | Indexing cost prohibitive for code (would be Contextual Retrieval all over again, worse). We do *structural* graphs from AST — cheaper, less expressive. |
| **Continue.dev / Cline** | IDE-side agents with bundled retrieval. | Different distribution channel. We are Claude-Code-native. |

**The discriminator that's actually defensible**: Lattice is the only one with *hook-lifecycle interception*. Pre-tool-use can **deny** a redundant `Read` and reroute the agent to `recall_expand`. Aider can't do that — its map is one-way prompt injection. Memory Tool can't — no hooks. Cursor can't — different runtime. This is the moat. Graph retrieval is table stakes in 2026; **hook-level token-flow control is not**.

---

## 2. OODA Re-Diagnosis

The prior OODA analysis is correct as far as it goes, but it analyzed *variants of Lattice*. Here it is rerun against the **competitive frontier**:

### Observe — signal ingest economics
- **Aider repo-map**: O(file) tree-sitter pass + PageRank, no embeddings. Sub-second.
- **Lattice HippoRAG**: O(file) tree-sitter + embeddings (FastEmbed) + PPR. ~15s for 695 files.
- **Cursor**: cloud-side, latency hidden behind network.
- **Contextual (TS)**: ~40min API-tax indexing.

**Read**: Aider is cheaper at ingest because it skips embeddings entirely. Lattice's embedding step is justified *only* if it materially beats Aider on retrieval quality. We have not benchmarked this. **Open hole.**

### Orient — contextual framing quality
- **Aider**: PageRank-ranked symbol map of the whole repo, refreshed each turn. Always-on background context, no agent query needed.
- **Lattice**: agent-pulled. The agent must know to call `recall`. SKILL.md helps but routing burden is on the model.
- **Cursor**: implicit, IDE-context-aware.

**Read**: Aider's *push* model and Lattice's *pull* model are different philosophies. Aider eats prompt budget continuously; Lattice eats it only on demand. On long sessions with many turns covering the same area, Aider's overhead amortizes; Lattice's grows with question count. **Hybrid is possible**: session-start hook emits a compressed PageRank map (Aider-style) + on-demand recall (Lattice-style). This is the architectural unification move.

### Decide — action pathing under uncertainty
The OODA finding: Lattice Base causes a **27-turn over-engagement loop**. Root cause: the agent gets weak, undifferentiated recall results and re-queries to compensate. The `recall` response doesn't tell it *when to stop*.

This is fixable with a single response-schema change: every `recall` response carries `confidence: high | medium | low` + `suggested_next_action: expand | grep | stop`. The agent's prompt cache will learn to trust the routing. This is cheaper than swapping retrieval engines.

### Act — execution and cache economy
HippoRAG: **+24.4%** vs baseline; **−46.5%** cache creation tokens vs Contextual.
Contextual: **−27.8%** vs baseline; 9-turn dominance but cache-taxed.
Lattice Base: **−19.3%** vs baseline. The default is the worst configuration.

---

## 3. The Self-Debate — 4 Architectural Moves

### Move 1 — Flip the default to HippoRAG (gated on N≥3 fixture validation)

**For**: HippoRAG wins on every Act-axis metric and resolves the over-engagement loop. The shipped default loses to no-plugin; this is a product emergency.

**Against**: One fixture. The fixture is TypeScript-heavy with dense import graphs — PageRank shines exactly where dense edges exist. Sparse repos (e.g. Go with internal packages, monorepos with weak cross-module links) may not see the same lift; could regress. Also adds `numpy`/`scipy` dependency surface to the default install.

**Resolution**: **Don't flip default yet.** Stand up an *adversarially-chosen* fixture set first (see §6 for the selection rationale). Concretely:
- Add 3 fixture corpora to `bench/fixtures/`, one from each of these buckets:
  - **Memorization-contaminated OSS** (e.g. FastAPI, Django, or Next.js) — to surface baseline inflation from training priors. If HippoRAG still wins here, the result is robust to memorization bias.
  - **Recent / obscure OSS** (a repo published post-training-cutoff or with low GitHub stars) — clean baseline, no memorized priors.
  - **Private/proprietary** (the existing `uniacco-site` qualifies, or a parallel one) — represents the actual user workload shape.
- Pick **at least one** Python repo and **at least one** Go repo across the three buckets so language coverage isn't pure TS.
- Re-run leverage benchmark across all four (current + 3 new). Decision rule: if HippoRAG wins ≥3/4 fixtures with positive cost leverage **including the memorization-contaminated one**, flip default in v0.2. If it only wins on the private repo, narrow the marketing claim to "private-repo workloads" — don't flip default.
- Until then: surface HippoRAG more loudly — `lattice doctor` should print *"You are running TS Base. HippoRAG won +24.4% on uniacco-site (n=1); consider `LATTICE_HIPPORAG=on`."*

**This single move converts the most damning finding (default is regressive) into a roadmap, without overclaiming.**

### Move 2 — Confidence-routed recall responses (the OODA fix)

**For**: The 27-turn over-engagement loop is a decision-pathing failure, not a retrieval failure. Every `recall` should return structured `confidence` + `suggested_next_action`. The agent's prompt cache learns to honor it. This is a sub-100-LOC change and is **strictly additive** — no behavior regression possible. Addresses the loop without re-architecting.

**Against**: We're guessing the agent will obey the hint. SKILL.md already gives routing guidance and the base config still loops. Suggests the LLM doesn't trust low-priority guidance unless it's load-bearing in the response shape.

**Counter-counter**: SKILL.md is *prompt-level prose*; a structured `confidence` field in every response is *response-level data* — qualitatively different cache signal. Worth one experiment.

**Resolution**: **Do it.** Add to the v0.1.x line as `LATTICE_CONFIDENCE_ROUTING=on` flag; benchmark on the same `p1`/`p2`/`p3` leverage suite; if turn count drops ≥15% on Base config, ship default.

### Move 3 — Hybrid push/pull context: session-start repo-map

**For**: Aider's competitive advantage is *push* context — every turn carries the most important symbols, free of agent query cost. Lattice already has `session-start` hook and a PageRank engine. Emit a 1-2k token Aider-style condensed map at session start. Agent has orientation immediately; cuts the first 3-5 exploration turns. Combined with on-demand `recall`, this is *strictly better* than either pure-push or pure-pull.

**Against**: Adds ~1-2k tokens to every session start, regardless of whether the agent needs them. For short sessions (small fixes), this is dead weight. Also hurts Bench 1 (tool/schema overhead) on paper — though that benchmark doesn't currently measure session-start token cost.

**Resolution**: **Do it, but make it adaptive.** Cap at 1.5k tokens. Skip emission if `git diff` shows the agent is likely on a focused fix (1-3 files changed since last session). Track impact via a new bench: *cold-start turns-to-first-edit*.

### Move 4 — Repair the quality problem (RAGAS faithfulness 0.327)

**For**: This is the unaddressed elephant. Three diagnoses possible:
- **Chunks are too coarse**: 2048-char target chunks mix multiple concepts. Reranker can't extract the relevant one. Fix: tighter chunks (1024 chars) + late-interaction reranking on sub-spans.
- **Embeddings are wrong model**: FastEmbed default is generic. Code-specialized models (Voyage `voyage-code-3`, Jina `jina-embeddings-v2-base-code`, Salesforce CodeRankEmbed) measurably beat generic on code retrieval per published evals.
- **No provenance signal**: chunks don't carry "why I'm here" — agent can't validate relevance, doesn't cite. Fix: every chunk response carries `evidence: {imports: [...], called_by: [...], superseded: ...}` so the model can self-validate before using.

**Against**: All three are real engineering work. Swapping embedders may require re-indexing, may have license issues (Voyage is paid), may not actually move R@K in the direction we hope. Faithfulness 0.327 may be measuring *RAGAS judge weirdness* on a 10-query synthetic fixture, not real chunk quality — we should sanity-check the fixture before chasing the number.

**Resolution**: **Diagnose first, fix second.**
1. Audit the 10 RAGAS queries — are they reasonable? Does a human reviewer agree faithfulness is low? (1 hour)
2. If real: A/B-bench three embedders on `retrieval-sanity` — pick the winner. (1 day)
3. Add `evidence` fields to chunk responses unconditionally. (1 day, low risk)
4. Re-run RAGAS. Gate the SOTA claim on faithfulness ≥ 0.7.

---

## 4. Moves I'm *Not* Recommending (and why)

- **Cheap contextual retrieval via Haiku 4.5**: tempting (Contextual got 9 turns!) but cache-bloat is structural — appending summaries to chunk bodies inflates every cache write. Even with free indexing it still loses on cost. Skip unless cache compaction (storing summaries in embedding space only, not chunk body) is engineered first — and that's a major refactor.
- **Differential AST re-indexing**: nice-to-have, but not on the critical path. Indexing already takes 15s; agent doesn't wait on it.
- **Negative cache for failed queries**: low impact, can defer.
- **Beating Cursor at scale**: out of scope, wrong target audience.

---

## 5. Decision Summary

| # | Move | Status | Gate | Effort | Risk |
|---|---|---|---|---|---|
| 1 | Flip default to HippoRAG | **Hold** | N≥3 fixture validation | 1 week (fixtures + reruns) | Low if gated |
| 2 | Confidence-routed responses | **Build** | Bench drop ≥15% turns | 2 days | Low (additive) |
| 3 | Session-start repo-map (Aider hybrid) | **Build** | New cold-start bench | 3 days | Low |
| 4 | Quality repair (chunks/embedder/evidence) | **Diagnose** | RAGAS faithfulness ≥0.7 | 1 week | Medium |

**Ordering**: 2 → 4 (diagnose) → 3 → 1 (fixture expansion in parallel). Move 2 first because it's the cheapest, most additive, addresses the OODA root cause directly, and tells us whether response-schema interventions move the needle.

---

## 6. What Would Actually Earn a "SOTA" Claim

The honest answer: **nothing in this doc**, until:

1. ≥3 **adversarially-chosen** fixture corpora all show positive cost leverage on the same default config. Adversarial selection means at least one fixture from each of:
   - **Memorization-contaminated** (well-known OSS Claude has trained on) — guards against baseline inflation
   - **Clean / post-cutoff** (obscure or recent OSS) — clean comparison
   - **Private / proprietary** — represents real user workload
   "Three popular Python repos" is **not** N=3 for this purpose — it's N=1 disguised as N=3, because they share the same memorization-bias direction.
2. RAGAS faithfulness > 0.7 (currently 0.327) — and re-run on a corpus that *isn't* in training (the current synthetic-fixture corpus is unaudited; if it's FastAPI-based, the judge's priors confound the score).
3. Head-to-head leverage benchmark against **Aider repo-map** (not Serena) on the same fixtures.
4. At least one independent third-party reproduces the numbers.

Until then the defensible claim is: *"Lattice is the only token-reducing layer for Claude Code with hook-level read interception. On a TypeScript corpus, the HippoRAG configuration delivers +24.4% cost leverage vs no-plugin baseline. The shipped default configuration currently underperforms baseline and is being reworked."* That's narrow, true, and a credible foundation. SOTA is earned, not claimed.

---

## 7. The Moat — Restated

Stop benchmarking against Serena. Start benchmarking against Aider. The graph-retrieval primitives are commodity. The defensible moat is:

- **Pre-tool-use interception** — deny redundant reads, redirect to recall. Aider can't.
- **Auto-capture of shell output** — turns one-shot bash into searchable memory. Aider can't.
- **Freshness decay + supersedes** — solves the staleness problem most RAGs ignore. Aider doesn't.
- **MCP-native** — clean tool surface, not prompt injection. Composable with Anthropic Memory Tool.

Lean here. The graph engine is a feature; the hook lifecycle is the product.
