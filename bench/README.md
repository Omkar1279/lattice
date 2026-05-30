# bench/

Operational benchmark suite for lattice-python. Spec lives in
[`../docs/benchmarking.md`](../docs/benchmarking.md). Result JSONs are
committed to this tree — the diff history is the audit trail.

## Layout

```
bench/
├── fixture.txt          # pinned commit SHA of the test repo (fastapi)
├── fixtures/            # ground-truth inputs (queries.yaml, ragas-queries.yaml)
├── runners/             # one file per benchmark (validate-tokenizer.py, ...)
└── results/             # flat directory of JSON outputs from runners
```

## Running

Each runner is a standard Python script. From the repo root:

```bash
# §3 Step 0 — validate that tiktoken + correction factor agrees with the
# Claude CLI count-tokens for Claude 4. MUST be done before any other bench work.
uv run python bench/runners/validate-tokenizer.py
```

## Bench inventory

| Script | What | Cost |
|---|---|---|
| `bench/runners/validate-tokenizer.py` | §3 Step 0 tokenizer agreement check | free / 2 CLI calls |
| `bench/runners/calibrate-tokenizer.py` | 30-sample calibration → `CLAUDE4_TOKENIZER_CORRECTION` | free / 31 CLI calls |
| `bench/runners/tool-overhead.py` | Bench 1: tool-schema bytes per server | free (claude-cli) |
| `bench/runners/freshness.py` | Bench 4: 10 freshness/supersedes scenarios (10/10 pass) | free |
| `bench/runners/retrieval-sanity.py` | Bench 2: FastAPI v0 20-query corpus | free baseline |
| `bench/runners/ragas.py` | RAGAS triad on local 10-query fixture | free |

## Reproducibility envelope

Every result file embeds a `meta` block (date, python version, platform, tokenizer) per `docs/benchmarking.md §1.4`. Without those fields, a number can't be defended a month later.
