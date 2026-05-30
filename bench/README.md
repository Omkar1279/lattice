# bench/

Operational benchmark suite for lattice. Spec lives in
[`../docs/benchmarking.md`](../docs/benchmarking.md). Result JSONs are
committed to this tree — the diff history is the audit trail.

## Layout

```
bench/
├── fixture.txt          # pinned commit SHA of the test repo (fastapi)
├── fixtures/            # ground-truth inputs (queries.yaml, tasks.yaml)
├── runners/             # one file per benchmark (validate-tokenizer.ts, ...)
└── results/
    └── YYYY-MM-DD/      # one directory per run, JSON outputs inside
```

## Running

Each runner is a `tsx` script. From the repo root:

```bash
# §3 Step 0 — validate that @anthropic-ai/tokenizer agrees with the
# Anthropic count-tokens API for Claude 4. MUST be done before any
# other bench work.
ANTHROPIC_API_KEY=sk-ant-... npx tsx bench/runners/validate-tokenizer.ts
```

Without `ANTHROPIC_API_KEY`, the validator still runs the local half and
writes a JSON noting `api_skipped`. Useful for smoke testing the pipeline,
but not sufficient to ship the bench numbers.

## Bench inventory (npm scripts)

| Script | What | Cost |
|---|---|---|
| `bench:validate-tokenizer` | §3 Step 0 tokenizer agreement check | free / 1 API call |
| `bench:calibrate-tokenizer` | 30-sample calibration → `CLAUDE4_TOKENIZER_CORRECTION` | free / 30 API calls |
| `bench:tool-overhead` | Bench 1: tool-schema bytes per server | free (claude-cli) |
| `bench:freshness` | Bench 4: 10 freshness/supersedes scenarios (10/10 as of 2026-05-27) | free |
| `bench:retrieval-sanity` | Bench 2: FastAPI v0 20-query corpus | free baseline; ~$0.12 for `LATTICE_CONTEXTUAL_CHUNKS=on` (one-time, cached) |
| `bench:ragas` | RAGAS triad on local 10-query fixture | free |
| `bench:coderag-humaneval` | Bench 6: 164 HumanEval problems, real published data | free (auto-downloads JSONL on first run) |
| `bench:retrieval-smoke` | 5-case synthetic CodeRAG-Bench-shaped smoke (NOT the published dataset) | free |

## Reproducibility envelope

Every result file embeds a `meta` block (date, lattice SHA, fixture SHA,
model, tokenizer, node, platform) per `docs/benchmarking.md §1.4`. Without
those fields, a number can't be defended a month later.
