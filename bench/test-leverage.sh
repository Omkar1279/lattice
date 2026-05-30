#!/usr/bin/env bash
# test-leverage.sh — A/B leverage benchmark for the lattice plugin.
#
# Default fixture: /Users/user/Documents/work/uniacco-site (private, ~709
# TS/TSX files, zero training-data contamination, typed-edge graph).
# Set LATTICE_BENCH_PUBLIC=1 to fall back to FastAPI (smaller, reproducible).
#
# Each pair runs an identical prompt twice — once WITH lattice (full subtree
# indexed, blocking active) and once WITHOUT — then diffs input_tokens,
# cache_creation_input_tokens, output_tokens, num_turns, total_cost_usd.
#
# Pairs measure scenarios that exercise lattice's documented value props:
#   1 (cross-session)     : two sessions per variant. Tests whether the
#                           session-start summary / recall avoids re-Read.
#   2 (corpus-scale)      : find a concept across the tree. Without lattice
#                           the model Greps a 700+ file repo and ingests
#                           verbose match lists; with lattice, recall returns
#                           a ranked chunk.
#   3 (graph-nav)         : "who uses X" — without lattice this is grep -rln
#                           + N Reads; with lattice it's recall +
#                           recall_expand(mode="callers").
#   4 (decision-persist)  : session A persists a fact via lattice.write,
#                           session B asks for it back — without lattice
#                           the answer is unrecoverable.
#
# ENV overrides:
#   BENCH_REPO_DIR        default /Users/user/Documents/work/uniacco-site
#   LATTICE_BENCH_PUBLIC  1 → use FastAPI (cloned to ~/.cache/lattice-bench)
#   BENCH_MODEL           default sonnet
#   BENCH_EFFORT          default medium
#   BENCH_MAX_BUDGET      default 1.50
#   BENCH_PAIRS           default "1 2 3 4"

set -eo pipefail

LATTICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_REPO="/Users/user/Documents/work/uniacco-site"
PUBLIC_REPO_URL="https://github.com/fastapi/fastapi.git"
PUBLIC_REPO_TAG="0.115.0"
PUBLIC_REPO_DIR="$HOME/.cache/lattice-bench/fastapi"

if [ "${LATTICE_BENCH_PUBLIC:-0}" = "1" ]; then
  BENCH_REPO_DIR="$PUBLIC_REPO_DIR"
  INDEX_DIRS=("fastapi")
else
  BENCH_REPO_DIR="${BENCH_REPO_DIR:-$DEFAULT_REPO}"
  INDEX_DIRS=("src")
fi

RESULTS_DIR="$LATTICE_ROOT/bench/results/leverage-$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$RESULTS_DIR/harness.log"
MODEL="${BENCH_MODEL:-sonnet}"
EFFORT="${BENCH_EFFORT:-medium}"
MAX_BUDGET="${BENCH_MAX_BUDGET:-1.50}"
PAIRS="${BENCH_PAIRS:-1 2 3 4}"
BENCH_RUNS="${BENCH_RUNS:-5}"

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

  log "    cost=\$$cost turns=$turns input=$in_tok cache_create=$cache_create cache_read=$cache_read output=$out_tok dur=${dur}s"
}

# Sum two single-session JSONs into a synthetic combined record so the
# summary loop can treat the cross-session pair uniformly.
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
json.dump(combined, open(out, "w"), indent=2)
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# PAIRS
# ─────────────────────────────────────────────────────────────────────────────

# PAIR 1 — cross-session continuity. Two sessions with --no-session-persistence
# so the prompt cache cannot carry over. WITHOUT lattice, S2 must rediscover
# what S1 already explored; WITH lattice, the index persists.
P1_S1='Look at this codebase and tell me, in one sentence, what the main application entry / root file does. Include the file path.'
P1_S2='What was the file path of the application entry / root file in this codebase? Be brief.'

run_pair1_crosssession() {
  log ""
  log "═══ PAIR 1 — cross-session continuity ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS ---"
    prep_lattice_tree
    # Reset telemetry log before first session
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "p1_s1" "$P1_S1" "$r" || true
    
    # We do not reset the lattice tree or telemetry log between S1 and S2
    # but we can optionally reset telemetry.log to isolate S2 telemetry,
    # or keep it. Let's reset it so S2 has its own telemetry, merged by sum_sessions.
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "p1_s2" "$P1_S2" "$r" || true
    
    [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
      cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/p1__hook__run${r}.log"
    clear_lattice
    run_variant "without" "p1_s1" "$P1_S1" "$r" || true
    run_variant "without" "p1_s2" "$P1_S2" "$r" || true
    for v in with without; do
      sum_sessions "$RESULTS_DIR/p1_s1__${v}__run${r}.json" \
                   "$RESULTS_DIR/p1_s2__${v}__run${r}.json" \
                   "$RESULTS_DIR/p1_crosssession__${v}__run${r}.json"
    done
  done
}

# PAIR 2 — corpus-scale. The model has to find a specific concept across a
# 700+ file repo. Without lattice it Greps and ingests the long match list;
# with lattice, recall returns a small ranked set.
P2_PROMPT='In this codebase, find where the global authentication or user-session context is initialised and provided to the rest of the app. Give me the file path and a one-sentence summary of how it works. Be efficient — minimise tool calls.'

# PAIR 3 — graph navigation. "Who uses X" is grep -rln + N Reads without
# lattice; with lattice it is recall + recall_expand(mode="callers").
P3_PROMPT='Find a widely-imported component or utility in this codebase. Tell me its name, its path, and list 3 other files that import or use it.'

# PAIR 4 — decision persistence. S1 asks the model to remember a decision;
# S2 (separate session) asks it back. Without lattice, S2 has no way to
# know — the only "memory" is whether the model wrote a stray .md file.
P4_S1='I want you to remember this decision for future sessions: "We chose React Query over SWR for data fetching because of its devtools and stale-while-revalidate semantics." Persist this so a future session can recall it. Confirm in one sentence.'
P4_S2='In an earlier session I asked you to remember a decision about a data-fetching library. What was the decision, and what was the stated reason?'

run_pair_simple() {
  local pair="$1" prompt="$2"
  log ""
  log "═══ ${pair} ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS ---"
    prep_lattice_tree
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "$pair" "$prompt" "$r" || true
    [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
      cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/${pair}__hook__run${r}.log"
    clear_lattice
    run_variant "without" "$pair" "$prompt" "$r" || true
  done
}

run_pair4_decisionpersist() {
  log ""
  log "═══ PAIR 4 — decision persistence ═══"
  for r in $(seq 1 "$BENCH_RUNS"); do
    log "--- Run $r/$BENCH_RUNS ---"
    prep_lattice_tree
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "p4_s1" "$P4_S1" "$r" || true
    
    rm -f "$WORK_REPO/.lattice/log/telemetry.log"
    run_variant "with" "p4_s2" "$P4_S2" "$r" || true
    
    [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
      cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/p4__hook__run${r}.log"
    clear_lattice
    run_variant "without" "p4_s1" "$P4_S1" "$r" || true
    run_variant "without" "p4_s2" "$P4_S2" "$r" || true
    for v in with without; do
      sum_sessions "$RESULTS_DIR/p4_s1__${v}__run${r}.json" \
                   "$RESULTS_DIR/p4_s2__${v}__run${r}.json" \
                   "$RESULTS_DIR/p4_decisionpersist__${v}__run${r}.json" \
                   "S1+S2 — inspect S2 output to judge correctness, not just cost"
    done
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

log "═══ LEVERAGE BENCHMARK ═══"
log "Lattice root: $LATTICE_ROOT"
log "Model:        $MODEL ($EFFORT)"
log "Pairs:        $PAIRS"
log "Results:      $RESULTS_DIR"

ensure_repo

for p in $PAIRS; do
  case "$p" in
    1) run_pair1_crosssession ;;
    2) run_pair_simple "p2_corpusscale" "$P2_PROMPT" ;;
    3) run_pair_simple "p3_graphnav"    "$P3_PROMPT" ;;
    4) run_pair4_decisionpersist ;;
    *) log "Unknown pair: $p" ;;
  esac
done

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

log ""
python3 - "$RESULTS_DIR" <<'PYEOF' | tee -a "$LOG_FILE"
import os
import sys
import glob
import json
import math

results_dir = sys.argv[1]

# Find all run files
files = glob.glob(os.path.join(results_dir, "*__*__run*.json"))

# Group files by (pair, variant)
groups = {}
for f in files:
    base = os.path.basename(f)
    parts = base.rsplit(".", 1)[0].split("__")
    if len(parts) != 3:
        continue
    pair, variant, run_str = parts
    groups.setdefault((pair, variant), []).append(f)

# Student's t-value for df = n - 1 = 4 at 95% confidence level
T_VALUE = 2.776

def compute_stats(values):
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    std_err = math.sqrt(variance) / math.sqrt(n)
    ci = T_VALUE * std_err
    return mean, ci

print("═══ LEVERAGE TABLE (n=5, mean ± 95% CI) ═══")
print(f"{'pair':<20} | {'variant':<7} | {'cost ($)':<18} | {'turns':<12} | {'input_tok':<18} | {'cache_create':<18} | {'cache_read':<18} | {'cache_hit %':<16} | {'output':<15} | {'recall_expand modes'}")
print("-" * 175)

# Sort keys for consistent display
sorted_keys = sorted(groups.keys(), key=lambda x: (x[0], x[1] == 'without'))

for pair, variant in sorted_keys:
    group_files = groups[(pair, variant)]
    costs, turns, inputs, creations, reads, hits, outputs = [], [], [], [], [], [], []
    telemetry_totals = {}
    
    n_runs = len(group_files)
    
    for gf in group_files:
        try:
            data = json.load(open(gf))
            costs.append(data.get("total_cost_usd", 0.0))
            turns.append(data.get("num_turns", 0))
            
            usage = data.get("usage", {})
            in_tok = usage.get("input_tokens", 0)
            cc = usage.get("cache_creation_input_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            
            inputs.append(in_tok)
            creations.append(cc)
            reads.append(cr)
            outputs.append(out_tok)
            
            denom = cr + cc
            hit_ratio = (cr / denom * 100.0) if denom > 0 else 0.0
            hits.append(hit_ratio)
            
            telemetry = data.get("telemetry", {})
            for mode, count in telemetry.items():
                telemetry_totals[mode] = telemetry_totals.get(mode, 0) + count
        except Exception as e:
            pass
            
    mean_cost, ci_cost = compute_stats(costs)
    mean_turns, ci_turns = compute_stats(turns)
    mean_in, ci_in = compute_stats(inputs)
    mean_cc, ci_cc = compute_stats(creations)
    mean_cr, ci_cr = compute_stats(reads)
    mean_hit, ci_hit = compute_stats(hits)
    mean_out, ci_out = compute_stats(outputs)
    
    # Format recall_expand modes representation
    mode_parts = []
    if variant == "with" and telemetry_totals:
        for mode in sorted(telemetry_totals.keys()):
            avg_count = telemetry_totals[mode] / n_runs
            mode_parts.append(f"{mode}:{avg_count:.1f}")
        modes_str = ", ".join(mode_parts)
    elif variant == "with":
        modes_str = "none"
    else:
        modes_str = "-"
        
    cost_str = f"{mean_cost:.4f} ± {ci_cost:.4f}"
    turns_str = f"{mean_turns:.1f} ± {ci_turns:.1f}"
    in_str = f"{mean_in:.1f} ± {ci_in:.1f}"
    cc_str = f"{mean_cc:.1f} ± {ci_cc:.1f}"
    cr_str = f"{mean_cr:.1f} ± {ci_cr:.1f}"
    hit_str = f"{mean_hit:.1f}% ± {ci_hit:.1f}%"
    out_str = f"{mean_out:.1f} ± {ci_out:.1f}"
    
    print(f"{pair:<20} | {variant:<7} | {cost_str:<18} | {turns_str:<12} | {in_str:<18} | {cc_str:<18} | {cr_str:<18} | {hit_str:<16} | {out_str:<15} | {modes_str}")

# Compute deltas comparing the mean of with vs without
pairs = set(k[0] for k in groups.keys())
print("\n═══ PER-PAIR DELTAS (with vs without mean comparisons) ═══")
for p in sorted(pairs):
    if (p, "with") not in groups or (p, "without") not in groups:
        continue
    
    with_files = groups[(p, "with")]
    without_files = groups[(p, "without")]
    
    w_costs = [json.load(open(f)).get("total_cost_usd", 0.0) for f in with_files]
    wo_costs = [json.load(open(f)).get("total_cost_usd", 0.0) for f in without_files]
    
    w_turns = [json.load(open(f)).get("num_turns", 0) for f in with_files]
    wo_turns = [json.load(open(f)).get("num_turns", 0) for f in without_files]
    
    w_inputs = [json.load(open(f)).get("usage", {}).get("input_tokens", 0) for f in with_files]
    wo_inputs = [json.load(open(f)).get("usage", {}).get("input_tokens", 0) for f in without_files]
    
    w_cc = [json.load(open(f)).get("usage", {}).get("cache_creation_input_tokens", 0) for f in with_files]
    wo_cc = [json.load(open(f)).get("usage", {}).get("cache_creation_input_tokens", 0) for f in without_files]
    
    w_cr = [json.load(open(f)).get("usage", {}).get("cache_read_input_tokens", 0) for f in with_files]
    wo_cr = [json.load(open(f)).get("usage", {}).get("cache_read_input_tokens", 0) for f in without_files]
    
    w_outputs = [json.load(open(f)).get("usage", {}).get("output_tokens", 0) for f in with_files]
    wo_outputs = [json.load(open(f)).get("usage", {}).get("output_tokens", 0) for f in without_files]

    w_eff = [i + c for i, c in zip(w_inputs, w_cc)]
    wo_eff = [i + c for i, c in zip(wo_inputs, wo_cc)]

    def pct(w, wo): return 0.0 if wo == 0 else 100.0 * (wo - w) / wo
    
    m_w_cost, ci_w_cost = compute_stats(w_costs)
    m_wo_cost, ci_wo_cost = compute_stats(wo_costs)
    
    m_w_turns, ci_w_turns = compute_stats(w_turns)
    m_wo_turns, ci_wo_turns = compute_stats(wo_turns)
    
    m_w_in, ci_w_in = compute_stats(w_inputs)
    m_wo_in, ci_wo_in = compute_stats(wo_inputs)
    
    m_w_cc, ci_w_cc = compute_stats(w_cc)
    m_wo_cc, ci_wo_cc = compute_stats(wo_cc)

    m_w_cr, ci_w_cr = compute_stats(w_cr)
    m_wo_cr, ci_wo_cr = compute_stats(wo_cr)

    m_w_eff, ci_w_eff = compute_stats(w_eff)
    m_wo_eff, ci_wo_eff = compute_stats(wo_eff)
    
    m_w_out, ci_w_out = compute_stats(w_outputs)
    m_wo_out, ci_wo_out = compute_stats(wo_outputs)
    
    print(f"{p}:")
    print(f"  cost           : with={m_w_cost:.4f} ± {ci_w_cost:.4f}  without={m_wo_cost:.4f} ± {ci_wo_cost:.4f}  saved={pct(m_w_cost, m_wo_cost):+6.1f}%")
    print(f"  turns          : with={m_w_turns:.1f} ± {ci_w_turns:.1f}  without={m_wo_turns:.1f} ± {ci_wo_turns:.1f}  saved={pct(m_w_turns, m_wo_turns):+6.1f}%")
    print(f"  input_tokens   : with={m_w_in:.1f} ± {ci_w_in:.1f}  without={m_wo_in:.1f} ± {ci_wo_in:.1f}  saved={pct(m_w_in, m_wo_in):+6.1f}%")
    print(f"  cache_creation : with={m_w_cc:.1f} ± {ci_w_cc:.1f}  without={m_wo_cc:.1f} ± {ci_wo_cc:.1f}  saved={pct(m_w_cc, m_wo_cc):+6.1f}%")
    print(f"  cache_read     : with={m_w_cr:.1f} ± {ci_w_cr:.1f}  without={m_wo_cr:.1f} ± {ci_wo_cr:.1f}  saved={pct(m_w_cr, m_wo_cr):+6.1f}%")
    print(f"  effective_in   : with={m_w_eff:.1f} ± {ci_w_eff:.1f}  without={m_wo_eff:.1f} ± {ci_wo_eff:.1f}  saved={pct(m_w_eff, m_wo_eff):+6.1f}%")
    print(f"  output_tokens  : with={m_w_out:.1f} ± {ci_w_out:.1f}  without={m_wo_out:.1f} ± {ci_wo_out:.1f}  saved={pct(m_w_out, m_wo_out):+6.1f}%")
    print()
PYEOF

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
