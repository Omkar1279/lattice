#!/usr/bin/env bash
# test-leverage.sh — A/B leverage benchmark for the lattice plugin.
#
# Default fixture: /Users/user/Documents/work/uniacco-site (private, ~709
# TS/TSX files, zero training-data contamination, typed-edge graph).
# Set LATTICE_BENCH_PUBLIC=1 to fall back to FastAPI (smaller, reproducible).
#
# Each pair runs an identical prompt twice — once WITH lattice (full subtree
# indexed, blocking active) and once WITHOUT — then diffs input_tokens,
# cache_creation_input_tokens, output_tokens, num_turns, total_cost_usd,
# AND grades correctness against the fixture's oracle (bench/fixtures/<name>/
# oracle.yaml). The headline metric is cost-per-correct-answer, not raw cost.
#
# Pairs measure scenarios that exercise lattice's documented value props:
#   1 (cross-session)     : two sessions per variant. Tests whether the
#                           session-start summary / recall avoids re-Read.
#   2 (corpus-scale)      : find a named concept across the tree. Without
#                           lattice the model Greps a 700+ file repo and
#                           ingests verbose match lists; with lattice,
#                           recall returns a ranked chunk.
#   3 (graph-nav)         : "who uses X" for a FIXTURE-SPECIFIC named
#                           symbol (FastAPI: APIRouter). Without lattice
#                           this is grep -rln + N Reads; with lattice
#                           it is recall + recall_expand(mode="callers").
#   4 (decision-persist)  : session A persists a fact via lattice.write,
#                           session B asks for it back — without lattice
#                           the answer is unrecoverable.
#
# Prompts and oracles are loaded from bench/fixtures/<fixture>/oracle.yaml
# so they can be specialized per fixture without editing this script.
#
# ENV overrides:
#   BENCH_REPO_DIR        default /Users/user/Documents/work/uniacco-site
#   LATTICE_BENCH_PUBLIC  1 → use FastAPI (cloned to ~/.cache/lattice-bench)
#   BENCH_MODEL           default claude-sonnet-4-6 (pinned; do NOT use
#                         alias "sonnet" — it drifts as new models ship)
#   BENCH_JUDGE_MODEL     default claude-opus-4-7 (for narrative oracles)
#   BENCH_EFFORT          default medium
#   BENCH_MAX_BUDGET      default 1.50
#   BENCH_PAIRS           default "1 2 3 4"
#   BENCH_RUNS            default 5 (plan calls for 10 on gating runs)
#   BENCH_ORDER_SEED      default $RANDOM ; pin to reproduce arm ordering

set -eo pipefail

LATTICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_REPO="$HOME/Documents/work/uniacco-site"
PUBLIC_REPO_URL="https://github.com/fastapi/fastapi.git"
PUBLIC_REPO_TAG="0.115.0"
PUBLIC_REPO_DIR="$HOME/.cache/lattice-bench/fastapi"

if [ "${LATTICE_BENCH_PUBLIC:-0}" = "1" ]; then
  BENCH_REPO_DIR="$PUBLIC_REPO_DIR"
  FIXTURE_NAME="fastapi"
else
  BENCH_REPO_DIR="${BENCH_REPO_DIR:-$DEFAULT_REPO}"
  FIXTURE_NAME="${BENCH_FIXTURE_NAME:-uniacco-site}"
fi

ORACLE_FILE="$LATTICE_ROOT/bench/fixtures/$FIXTURE_NAME/oracle.yaml"
if [ ! -f "$ORACLE_FILE" ]; then
  echo "ERROR: no oracle file at $ORACLE_FILE — correctness scoring requires one. See bench/fixtures/fastapi/oracle.yaml for the schema." >&2
  exit 1
fi

# Load INDEX_DIRS from the oracle file (single source of truth per fixture).
INDEX_DIRS_RAW=$(python3 -c "
import yaml, sys
d = yaml.safe_load(open('$ORACLE_FILE'))
print(' '.join(d.get('index_dirs') or ['src']))
")
read -ra INDEX_DIRS <<< "$INDEX_DIRS_RAW"

RESULTS_DIR="$LATTICE_ROOT/bench/results/leverage-$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$RESULTS_DIR/harness.log"
# Pin to a concrete model ID so reruns are longitudinally comparable.
# "sonnet" is an alias that resolves to whatever the current Sonnet is — fine
# for users, fatal for benchmark reproducibility.
MODEL="${BENCH_MODEL:-claude-sonnet-4-6}"
export BENCH_JUDGE_MODEL="${BENCH_JUDGE_MODEL:-claude-opus-4-7}"
EFFORT="${BENCH_EFFORT:-medium}"
MAX_BUDGET="${BENCH_MAX_BUDGET:-1.50}"
PAIRS="${BENCH_PAIRS:-1 2 3 4}"
BENCH_RUNS="${BENCH_RUNS:-5}"
BENCH_ORDER_SEED="${BENCH_ORDER_SEED:-$RANDOM}"

mkdir -p "$RESULTS_DIR"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

# WORK_REPO is where `claude -p` actually runs. For the public FastAPI path
# it is the throwaway clone (safe to write into). For a private repo it is an
# isolated copy in a temp dir, so the model — which runs under
# --dangerously-skip-permissions and may write files (esp. pair 4 without
# lattice) — can NEVER touch the user's real working tree.
WORK_REPO=""
cleanup() { [ -n "$WORK_REPO" ] && [ "${WORK_REPO#/tmp/}" != "$WORK_REPO" ] && rm -rf "$WORK_REPO"; true; }
trap cleanup EXIT

ensure_repo() {
  if [ "${LATTICE_BENCH_PUBLIC:-0}" = "1" ]; then
    if [ ! -d "$PUBLIC_REPO_DIR/.git" ]; then
      log "Cloning $PUBLIC_REPO_URL @ $PUBLIC_REPO_TAG"
      mkdir -p "$(dirname "$PUBLIC_REPO_DIR")"
      git clone --depth 1 --branch "$PUBLIC_REPO_TAG" \
        "$PUBLIC_REPO_URL" "$PUBLIC_REPO_DIR" >>"$LOG_FILE" 2>&1
    fi
    WORK_REPO="$PUBLIC_REPO_DIR"
    log "Using repo: $WORK_REPO (throwaway clone, subdirs: ${INDEX_DIRS[*]})"
  else
    if [ ! -d "$BENCH_REPO_DIR" ]; then
      log "ERROR: repo not found: $BENCH_REPO_DIR"
      exit 1
    fi
    # Isolate: copy the repo (minus heavy/VCS dirs) so the benchmarked model
    # cannot contaminate the user's working tree.
    WORK_REPO="$(mktemp -d /tmp/lattice-bench-repo.XXXXXX)"
    log "Isolating $BENCH_REPO_DIR → $WORK_REPO (copying ${INDEX_DIRS[*]} + configs)"
    rsync -a \
      --exclude node_modules --exclude .git --exclude .next \
      --exclude dist --exclude build --exclude .lattice \
      "$BENCH_REPO_DIR"/ "$WORK_REPO"/ >>"$LOG_FILE" 2>&1
    log "Using repo: $WORK_REPO (isolated copy, subdirs: ${INDEX_DIRS[*]})"
  fi
}

# Index every source file under $INDEX_DIRS into a fresh .lattice vault,
# then backdate so PreToolUse blocking will trigger on subsequent reads.
prep_lattice_tree() {
  cd "$WORK_REPO"
  rm -rf .lattice
  mkdir -p .lattice/log .lattice/notes

  # Schema is automatically initialised in Python when opening the vault.
  python3 -c "
from lattice.storage.vault import open_vault
open_vault('$WORK_REPO/.lattice').close()
"

  local dirs_json
  dirs_json=$(printf '"%s",' "${INDEX_DIRS[@]}")
  dirs_json="[${dirs_json%,}]"

  cd "$LATTICE_ROOT"
  log "Indexing tree under ${INDEX_DIRS[*]}..."
  PYTHONPATH=. python3 -c "
import os
import json
from lattice.storage.vault import open_vault
from lattice.indexer.indexer import index_file, should_ignore
from lattice.indexer.graph import init_tree_sitter, resolve_and_write_edges

vault = open_vault('$WORK_REPO/.lattice')
init_tree_sitter()

index_dirs = $dirs_json
parseable_exts = {'.ts', '.tsx', '.js', '.jsx', '.py', '.rs', '.go'}

all_files = []
for d in index_dirs:
    dir_path = os.path.join('$WORK_REPO', d)
    if not os.path.exists(dir_path):
        continue
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [sub for sub in dirs if sub not in {
            'node_modules', '.git', '.terraform', '.next', '.nuxt',
            'dist', 'build', '.lattice', '.venv', '.pytest_cache', '__pycache__'
        }]
        for file in files:
            full_path = os.path.join(root, file)
            if should_ignore(full_path, '$WORK_REPO'):
                continue
            ext = os.path.splitext(file)[1]
            if ext in parseable_exts:
                all_files.append(full_path)

print(f'Indexing {len(all_files)} files...')
pending_edges = []
n = 0
for f in all_files:
    try:
        index_file(vault, f, '$WORK_REPO', pending_edges)
    except Exception:
        pass
    n += 1
    if n % 100 == 0:
        print(f'  {n}/{len(all_files)}')

for p in pending_edges:
    try:
        resolve_and_write_edges(vault, p['chunk_id'], p['file_path'], p['raw_edges'], '$WORK_REPO')
    except Exception:
        pass

vault.close()
print(f'Indexed {len(all_files)} files')
" 2>&1 | tee -a "$LOG_FILE"

  cd "$WORK_REPO"
  python3 -c "
import sqlite3
from datetime import datetime, timedelta, timezone
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
db = sqlite3.connect('.lattice/index.db')
db.execute('UPDATE chunks SET last_seen_at = ?', (yesterday,))
db.commit()
db.close()
" 2>&1 | tee -a "$LOG_FILE"
}

clear_lattice() { rm -rf "$WORK_REPO/.lattice"; }

# Load a pair's prompt from the fixture oracle YAML so prompts can be
# fixture-specific (e.g. P3 names APIRouter on FastAPI vs a uniacco symbol
# on uniacco-site). Fails loud if the key is missing — the caller relies on
# the prompt being non-empty. Supports query paraphrasing confounder control
# if 'paraphrases' list is present in the oracle.yaml.
load_prompt() {
  local pair_key="$1"
  local run_num="${2:-1}"
  python3 -c "
import yaml, sys
d = yaml.safe_load(open('$ORACLE_FILE'))
p = (d.get('pairs') or {}).get('$pair_key') or {}
paraphrases = p.get('paraphrases')
if isinstance(paraphrases, list) and len(paraphrases) > 0:
    idx = ($BENCH_ORDER_SEED + $run_num) % len(paraphrases)
    prompt = paraphrases[idx]
else:
    prompt = p.get('prompt')
if not prompt:
    sys.stderr.write('load_prompt: missing prompt or paraphrases for $pair_key in $ORACLE_FILE\n')
    sys.exit(2)
print(prompt.strip())
"
}

# Grade a result.json against the fixture's oracle for this pair. Appends
# _correctness to the JSON; never fails the run (scoring is best-effort).
# See bench/lib/score_answer.py for the schema.
score_answer() {
  local pair_key="$1" outfile="$2"
  [ -f "$outfile" ] || return 0
  PYTHONPATH="$LATTICE_ROOT" python3 -m bench.lib.score_answer \
    "$ORACLE_FILE" "$pair_key" "$outfile" >>"$LOG_FILE" 2>&1 || \
    log "    ⚠ score_answer failed (non-fatal) for $pair_key"
}

# Deterministically shuffle "with without" → e.g. "without with" given a
# (seed, run_num) pair. Eliminates the constant with-then-without ordering
# bias from the previous harness. Same (seed, run) → same order across reruns.
variant_order() {
  local run_num="$1"
  python3 -c "
import random
r = random.Random(($BENCH_ORDER_SEED * 100003) + $run_num)
xs = ['with', 'without']
r.shuffle(xs)
print(' '.join(xs))
"
}

# Run a single prompt with or without the plugin and record metrics
run_variant() {
  local variant="$1" pair="$2" prompt="$3" run_num="$4"
  local outfile="$RESULTS_DIR/${pair}__${variant}__run${run_num}.json"

  log "  ▶ $pair / $variant (run $run_num)"
  cd "$WORK_REPO"

  local plugin_args=()
  [ "$variant" = "with" ] && plugin_args=(--plugin-dir "$LATTICE_ROOT")

  local start exit_code=0
  start=$(date +%s)
  claude -p --model "$MODEL" --effort "$EFFORT" --output-format json \
    "${plugin_args[@]}" --dangerously-skip-permissions \
    --max-budget-usd "$MAX_BUDGET" --no-session-persistence \
    "$prompt" > "$outfile" 2>>"$LOG_FILE" || exit_code=$?

  local dur=$(( $(date +%s) - start ))
  if [ $exit_code -ne 0 ] || [ ! -s "$outfile" ]; then
    log "    ✗ failed (exit=$exit_code)"
    return 1
  fi

  # Extract telemetry from $WORK_REPO/.lattice/log/telemetry.log and add to $outfile
  if [ "$variant" = "with" ] && [ -f "$WORK_REPO/.lattice/log/telemetry.log" ]; then
    python3 - "$outfile" "$WORK_REPO/.lattice/log/telemetry.log" <<'PYEOF'
import json, sys
outfile, logfile = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(outfile))
    telemetry = {}
    with open(logfile, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith("recall_expand:"):
                mode = line.strip().split(":", 1)[1]
                telemetry[mode] = telemetry.get(mode, 0) + 1
    data["telemetry"] = telemetry
    json.dump(data, open(outfile, "w"), indent=2)
except Exception as e:
    sys.stderr.write(f"Error adding telemetry: {e}\n")
PYEOF
  fi

  local cost turns in_tok cache_create out_tok cache_read
  cost=$(jq -r '.total_cost_usd // 0' "$outfile")
  turns=$(jq -r '.num_turns // 0' "$outfile")
  in_tok=$(jq -r '.usage.input_tokens // 0' "$outfile")
  cache_create=$(jq -r '.usage.cache_creation_input_tokens // 0' "$outfile")
  cache_read=$(jq -r '.usage.cache_read_input_tokens // 0' "$outfile")
  out_tok=$(jq -r '.usage.output_tokens // 0' "$outfile")

  # Grade correctness against the fixture oracle. Pair key is the prefix
  # of the result filename ("p2_corpusscale", "p1_s1", etc.) — same as $pair.
  score_answer "$pair" "$outfile"
  local correct
  correct=$(jq -r '._correctness.passed // "unscored"' "$outfile")

  log "    cost=\$$cost turns=$turns input=$in_tok cache_create=$cache_create cache_read=$cache_read output=$out_tok correct=$correct dur=${dur}s"
}

# Sum two single-session JSONs into a synthetic combined record so the
# summary loop can treat the cross-session pair uniformly.
#
# The combined record inherits its `_correctness` from session B (S2). For
# cross-session and decision-persistence pairs, S2 is the test — it asks the
# model to recover what S1 established. S1's correctness is meaningful in
# its own row (p1_s1, p4_s1); the combined row's verdict is S2's. Without
# this propagation, the headline $/correct column is permanently "—" for
# p1_crosssession and p4_decisionpersist — i.e. silently absent from the
# rows that pairs 1 and 4 exist to measure.
sum_sessions() {
  local f1="$1" f2="$2" out="$3" note="${4:-S1+S2}"
  [ -f "$f1" ] && [ -f "$f2" ] || return 0
  python3 - "$f1" "$f2" "$out" "$note" <<'PYEOF'
import json, sys
f1, f2, out, note = sys.argv[1:5]
a = json.load(open(f1)); b = json.load(open(f2))
ua = a.get("usage", {}); ub = b.get("usage", {})
keys = ["input_tokens","cache_creation_input_tokens","cache_read_input_tokens","output_tokens"]
telemetry = {}
ta = a.get("telemetry", {})
tb = b.get("telemetry", {})
for k in set(list(ta.keys()) + list(tb.keys())):
    telemetry[k] = ta.get(k, 0) + tb.get(k, 0)
combined = {
    "total_cost_usd": a.get("total_cost_usd", 0) + b.get("total_cost_usd", 0),
    "num_turns": a.get("num_turns", 0) + b.get("num_turns", 0),
    "usage": {k: ua.get(k, 0) + ub.get(k, 0) for k in keys},
    "telemetry": telemetry,
    "_synthetic": note,
}
# Propagate S2's correctness verdict into the combined record. If S2 wasn't
# scored, fall back to S1 only if it was — the goal is to never silently
# lose a correctness signal we have.
b_corr = b.get("_correctness")
a_corr = a.get("_correctness")
if b_corr and b_corr.get("passed") in (0, 1):
    combined["_correctness"] = dict(b_corr)
    combined["_correctness"]["inherited_from"] = "S2"
elif a_corr and a_corr.get("passed") in (0, 1):
    combined["_correctness"] = dict(a_corr)
    combined["_correctness"]["inherited_from"] = "S1 (S2 unscored)"
json.dump(combined, open(out, "w"), indent=2)
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# PAIRS
#
# Prompts now live in bench/fixtures/<fixture>/oracle.yaml — see load_prompt().
# Each per-run cycle: prep_lattice_tree → run-variant-A → clear → run-variant-B,
# where variant order (A=with B=without OR A=without B=with) is shuffled per
# run from BENCH_ORDER_SEED. This removes the constant with-then-without
# ordering bias of the previous harness (cache-warmup effects no longer favour
# whichever variant ran first).
# ─────────────────────────────────────────────────────────────────────────────

# Helper: run a single variant for a multi-session pair (p1, p4). Prepares the
# vault if this variant uses lattice; clears it otherwise.
_run_variant_multisession() {
  local variant="$1" pair_s1="$2" pair_s2="$3" prompt_s1="$4" prompt_s2="$5" run_num="$6" pair_combined="$7"
  if [ "$variant" = "with" ]; then
    prep_lattice_tree
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "$pair_s1" "$prompt_s1" "$run_num" || true
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "$pair_s2" "$prompt_s2" "$run_num" || true
    [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
      cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/${pair_combined}__hook__run${run_num}.log"
    clear_lattice
  else
    clear_lattice
    run_variant "without" "$pair_s1" "$prompt_s1" "$run_num" || true
    run_variant "without" "$pair_s2" "$prompt_s2" "$run_num" || true
  fi
  sum_sessions "$RESULTS_DIR/${pair_s1}__${variant}__run${run_num}.json" \
               "$RESULTS_DIR/${pair_s2}__${variant}__run${run_num}.json" \
               "$RESULTS_DIR/${pair_combined}__${variant}__run${run_num}.json"
}

# Helper: run a single variant for a single-session pair (p2, p3).
_run_variant_singlesession() {
  local variant="$1" pair="$2" prompt="$3" run_num="$4"
  if [ "$variant" = "with" ]; then
    prep_lattice_tree
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "$pair" "$prompt" "$run_num" || true
    [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
      cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/${pair}__hook__run${run_num}.log"
    clear_lattice
  else
    clear_lattice
    run_variant "without" "$pair" "$prompt" "$run_num" || true
  fi
}

run_pair1_crosssession() {
  log ""
  log "═══ PAIR 1 — cross-session continuity ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS (order: $(variant_order $r)) ---"
    local p1_s1 p1_s2
    p1_s1=$(load_prompt "p1_s1" "$r")
    p1_s2=$(load_prompt "p1_s2" "$r")
    for v in $(variant_order $r); do
      _run_variant_multisession "$v" "p1_s1" "p1_s2" "$p1_s1" "$p1_s2" "$r" "p1_crosssession"
    done
  done
}

run_pair_simple() {
  local pair="$1"
  log ""
  log "═══ ${pair} ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS (order: $(variant_order $r)) ---"
    local prompt
    prompt=$(load_prompt "$pair" "$r")
    for v in $(variant_order $r); do
      _run_variant_singlesession "$v" "$pair" "$prompt" "$r"
    done
  done
}

run_pair4_decisionpersist() {
  log ""
  log "═══ PAIR 4 — decision persistence ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS (order: $(variant_order $r)) ---"
    local p4_s1 p4_s2
    p4_s1=$(load_prompt "p4_s1" "$r")
    p4_s2=$(load_prompt "p4_s2" "$r")
    for v in $(variant_order $r); do
      _run_variant_multisession "$v" "p4_s1" "p4_s2" "$p4_s1" "$p4_s2" "$r" "p4_decisionpersist"
    done
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

log "═══ LEVERAGE BENCHMARK ═══"
log "Lattice root: $LATTICE_ROOT"
log "Fixture:      $FIXTURE_NAME  (oracle: $ORACLE_FILE)"
log "Model:        $MODEL ($EFFORT)   judge: $BENCH_JUDGE_MODEL"
log "Pairs:        $PAIRS  runs/pair: $BENCH_RUNS  order_seed: $BENCH_ORDER_SEED"
log "Results:      $RESULTS_DIR"

ensure_repo

for p in $PAIRS; do
  case "$p" in
    1) run_pair1_crosssession ;;
    2) run_pair_simple "p2_corpusscale" ;;
    3) run_pair_simple "p3_graphnav" ;;
    4) run_pair4_decisionpersist ;;
    *) log "Unknown pair: $p" ;;
  esac
done

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY — median/IQR + Mann-Whitney U + cost-per-correct headline.
# See bench/lib/summarize_leverage.py for the methodology rationale.
# ─────────────────────────────────────────────────────────────────────────────

log ""
PYTHONPATH="$LATTICE_ROOT" python3 -m bench.lib.summarize_leverage \
  "$RESULTS_DIR" | tee -a "$LOG_FILE"

log "═══ HOOK ACTIVITY (WITH variant) ═══"
for r in $(seq 1 "$BENCH_RUNS"); do
  for h in "$RESULTS_DIR"/*__hook__run${r}.log; do
    [ -f "$h" ] || continue
    pair_run=$(basename "$h" .log)
    blocked=$(grep -c 'BLOCKED' "$h" 2>/dev/null || true)
    intercepted=$(grep -c 'intercepted:' "$h" 2>/dev/null || true)
    errors=$(grep -c 'error:' "$h" 2>/dev/null || true)
    log "  $pair_run: BLOCKED=$blocked  intercepted=$intercepted  errors=$errors"
  done
done

log ""
log "Results dir: $RESULTS_DIR"
