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
cleanup() { [ -n "$WORK_REPO" ] && [ "${WORK_REPO#/tmp/}" != "$WORK_REPO" ] && rm -rf "$WORK_REPO"; }
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
  local variant="$1" pair="$2" prompt="$3"
  local outfile="$RESULTS_DIR/${pair}__${variant}.json"

  log "  ▶ $pair / $variant"
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

  local cost turns in_tok cache_create out_tok
  cost=$(jq -r '.total_cost_usd // 0' "$outfile")
  turns=$(jq -r '.num_turns // 0' "$outfile")
  in_tok=$(jq -r '.usage.input_tokens // 0' "$outfile")
  cache_create=$(jq -r '.usage.cache_creation_input_tokens // 0' "$outfile")
  out_tok=$(jq -r '.usage.output_tokens // 0' "$outfile")

  log "    cost=\$$cost turns=$turns input=$in_tok cache_create=$cache_create output=$out_tok dur=${dur}s"
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
combined = {
    "total_cost_usd": a.get("total_cost_usd", 0) + b.get("total_cost_usd", 0),
    "num_turns": a.get("num_turns", 0) + b.get("num_turns", 0),
    "usage": {k: ua.get(k, 0) + ub.get(k, 0) for k in keys},
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
  prep_lattice_tree
  run_variant "with" "p1_s1" "$P1_S1" || true
  run_variant "with" "p1_s2" "$P1_S2" || true
  [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
    cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/p1__hook.log"
  clear_lattice
  run_variant "without" "p1_s1" "$P1_S1" || true
  run_variant "without" "p1_s2" "$P1_S2" || true
  for v in with without; do
    sum_sessions "$RESULTS_DIR/p1_s1__${v}.json" \
                 "$RESULTS_DIR/p1_s2__${v}.json" \
                 "$RESULTS_DIR/p1_crosssession__${v}.json"
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
  prep_lattice_tree
  run_variant "with" "$pair" "$prompt" || true
  [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
    cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/${pair}__hook.log"
  clear_lattice
  run_variant "without" "$pair" "$prompt" || true
}

run_pair4_decisionpersist() {
  log ""
  log "═══ PAIR 4 — decision persistence ═══"
  prep_lattice_tree
  run_variant "with" "p4_s1" "$P4_S1" || true
  run_variant "with" "p4_s2" "$P4_S2" || true
  [ -f "$WORK_REPO/.lattice/log/hook.log" ] && \
    cp "$WORK_REPO/.lattice/log/hook.log" "$RESULTS_DIR/p4__hook.log"
  clear_lattice
  run_variant "without" "p4_s1" "$P4_S1" || true
  run_variant "without" "p4_s2" "$P4_S2" || true
  for v in with without; do
    sum_sessions "$RESULTS_DIR/p4_s1__${v}.json" \
                 "$RESULTS_DIR/p4_s2__${v}.json" \
                 "$RESULTS_DIR/p4_decisionpersist__${v}.json" \
                 "S1+S2 — inspect S2 output to judge correctness, not just cost"
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
log "═══ LEVERAGE TABLE ═══"
printf "%-26s | %-7s | %8s | %5s | %9s | %12s | %8s\n" \
  "pair" "variant" "cost(\$)" "turns" "input_tok" "cache_create" "output" | tee -a "$LOG_FILE"
printf -- "---------------------------+---------+----------+-------+-----------+--------------+----------\n" \
  | tee -a "$LOG_FILE"

for f in "$RESULTS_DIR"/*__with.json "$RESULTS_DIR"/*__without.json; do
  [ -f "$f" ] || continue
  base=$(basename "$f" .json)
  pair="${base%__*}"; variant="${base##*__}"
  cost=$(jq -r '.total_cost_usd // 0' "$f")
  turns=$(jq -r '.num_turns // 0' "$f")
  in_tok=$(jq -r '.usage.input_tokens // 0' "$f")
  cc=$(jq -r '.usage.cache_creation_input_tokens // 0' "$f")
  out_tok=$(jq -r '.usage.output_tokens // 0' "$f")
  printf "%-26s | %-7s | %8.4f | %5d | %9d | %12d | %8d\n" \
    "$pair" "$variant" "$cost" "$turns" "$in_tok" "$cc" "$out_tok" | tee -a "$LOG_FILE"
done

log ""
log "═══ PER-PAIR DELTAS (with vs without) ═══"
for fw in "$RESULTS_DIR"/*__with.json; do
  [ -f "$fw" ] || continue
  base=$(basename "$fw" __with.json)
  fwo="$RESULTS_DIR/${base}__without.json"
  [ -f "$fwo" ] || continue
  python3 - "$fw" "$fwo" "$base" <<'PYEOF' | tee -a "$LOG_FILE"
import json, sys
fw, fwo, name = sys.argv[1], sys.argv[2], sys.argv[3]
a = json.load(open(fw)); b = json.load(open(fwo))
ua = a.get("usage", {}); ub = b.get("usage", {})

def pct(w, wo): return 0.0 if wo == 0 else 100.0 * (wo - w) / wo

ca, cb = a.get("total_cost_usd",0), b.get("total_cost_usd",0)
ia, ib = ua.get("input_tokens",0), ub.get("input_tokens",0)
cca, ccb = ua.get("cache_creation_input_tokens",0), ub.get("cache_creation_input_tokens",0)
oa, ob = ua.get("output_tokens",0), ub.get("output_tokens",0)
ta, tb = a.get("num_turns",0), b.get("num_turns",0)
ea, eb = ia+cca, ib+ccb

print(f"{name}:")
print(f"  cost           : with=${ca:.4f}  without=${cb:.4f}  saved={pct(ca,cb):+6.1f}%")
print(f"  input_tokens   : with={ia:7d}    without={ib:7d}    saved={pct(ia,ib):+6.1f}%")
print(f"  cache_creation : with={cca:7d}    without={ccb:7d}    saved={pct(cca,ccb):+6.1f}%")
print(f"  effective_in   : with={ea:7d}    without={eb:7d}    saved={pct(ea,eb):+6.1f}%")
print(f"  output_tokens  : with={oa:7d}    without={ob:7d}    saved={pct(oa,ob):+6.1f}%")
print(f"  turns          : with={ta:7d}    without={tb:7d}")
print()
PYEOF
done

log "═══ HOOK ACTIVITY (WITH variant) ═══"
for h in "$RESULTS_DIR"/*__hook.log; do
  [ -f "$h" ] || continue
  pair=$(basename "$h" __hook.log)
  blocked=$(grep -c 'BLOCKED' "$h" 2>/dev/null || true)
  intercepted=$(grep -c 'intercepted:' "$h" 2>/dev/null || true)
  errors=$(grep -c 'error:' "$h" 2>/dev/null || true)
  log "  $pair: BLOCKED=$blocked  intercepted=$intercepted  errors=$errors"
done

log ""
log "Results dir: $RESULTS_DIR"
