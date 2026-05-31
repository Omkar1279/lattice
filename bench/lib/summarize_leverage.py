"""Summarize a leverage-bench results directory.

Replaces the inline bash heredoc with median/IQR + Mann-Whitney U +
cost-per-correct ranking. LLM cost/turn distributions are heavy-tailed;
mean ± Student's-t CI lied. Headline metric is now cost-per-correct-answer.

Usage:
    python3 -m bench.lib.summarize_leverage <results_dir>
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
from collections import defaultdict
from typing import Iterable


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def quartiles(xs: list[float]) -> tuple[float, float, float]:
    """Return (q1, median, q3) using the standard linear-interpolation method."""
    if not xs:
        return (0.0, 0.0, 0.0)
    s = sorted(xs)
    n = len(s)

    def _pct(p: float) -> float:
        # Type-7 quantile (numpy / R default).
        if n == 1:
            return s[0]
        h = (n - 1) * p
        lo, hi = int(math.floor(h)), int(math.ceil(h))
        if lo == hi:
            return s[lo]
        return s[lo] + (h - lo) * (s[hi] - s[lo])

    return (_pct(0.25), _pct(0.50), _pct(0.75))


def mann_whitney_u(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Two-sided Mann-Whitney U test with normal approximation.

    Returns (U, p_value). Approximation is reliable for n,m >= 5; tiny
    samples will have inflated p-values. We accept this — the harness
    targets n=10 per cell per the test plan.
    """
    n, m = len(xs), len(ys)
    if n == 0 or m == 0:
        return (0.0, 1.0)

    combined = [(v, 0) for v in xs] + [(v, 1) for v in ys]
    combined.sort(key=lambda t: t[0])

    # Average-rank assignment for ties (standard Mann-Whitney handling).
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1

    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = r1 - n * (n + 1) / 2
    u2 = n * m - u1
    u = min(u1, u2)

    mean_u = n * m / 2
    sd_u = math.sqrt(n * m * (n + m + 1) / 12)
    if sd_u == 0:
        return (u, 1.0)
    z = (u - mean_u) / sd_u
    # Two-sided p-value from normal approx.
    p = 2 * (1 - _phi(abs(z)))
    return (u, max(0.0, min(1.0, p)))


def _phi(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun 26.2.17)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


_CLEARLY_BETTER_MIN_N = 3


def clearly_better(better: list[float], worse: list[float]) -> bool:
    """Non-parametric 'clearly better' rule from SYSTEMATIC_TEST_PLAN.md §3:
    median of `better` is lower AND its 75th percentile is below the 25th
    percentile of `worse`. Used for cost/turns where smaller is better.

    Requires n>=3 in BOTH samples — with n<3 the quartiles collapse onto
    the median, so the rule trivially passes whenever the medians differ,
    which is meaningless signal. Returns False for under-powered cells.
    """
    if len(better) < _CLEARLY_BETTER_MIN_N or len(worse) < _CLEARLY_BETTER_MIN_N:
        return False
    _, m_b, q3_b = quartiles(better)
    q1_w, m_w, _ = quartiles(worse)
    return m_b < m_w and q3_b < q1_w


def _load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _is_run_successful(data: dict) -> bool:
    """A run is excluded from stats if it's missing core telemetry."""
    if not data:
        return False
    if data.get("total_cost_usd") in (None, 0) and data.get("num_turns") in (None, 0):
        return False
    return True


def collect(results_dir: str) -> dict:
    """Returns {(pair, variant): [run_data, ...]}.

    Accepts both filename forms:
      - new: pair__variant__runN.json  (current harness, BENCH_RUNS aware)
      - old: pair__variant.json        (legacy single-run dumps)
    Old runs are treated as a single n=1 sample so historical leverage-*
    dirs remain summarizable.
    """
    files = glob.glob(os.path.join(results_dir, "*__*.json"))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in files:
        base = os.path.basename(f).rsplit(".", 1)[0]
        parts = base.split("__")
        if len(parts) == 3:
            pair, variant, _run = parts
        elif len(parts) == 2:
            pair, variant = parts
        else:
            continue
        if variant not in ("with", "without"):
            continue
        d = _load(f)
        if d is None:
            continue
        d["_source_file"] = base
        groups[(pair, variant)].append(d)
    return groups


def _extract(runs: Iterable[dict], path: tuple[str, ...], default=0):
    """Extract nested key like ('usage', 'input_tokens')."""
    out = []
    for r in runs:
        cur: object = r
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        out.append(cur if cur is not None else default)
    return out


def correctness_rate(runs: list[dict]) -> tuple[float | None, int, int]:
    """Returns (rate, n_scored, n_total). rate is None if nothing scored."""
    scored = [r for r in runs if (r.get("_correctness") or {}).get("passed") in (0, 1)]
    if not scored:
        return (None, 0, len(runs))
    passed = sum(1 for r in scored if r["_correctness"]["passed"] == 1)
    return (passed / len(scored), len(scored), len(runs))


def fmt_iqr(xs: list[float], fmt: str = "{:.4f}") -> str:
    if not xs:
        return "—"
    q1, med, q3 = quartiles(xs)
    return f"{fmt.format(med)} [{fmt.format(q1)}–{fmt.format(q3)}]"


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def print_report(groups: dict) -> None:
    print("\n═══ LEVERAGE TABLE (median [IQR]; n_scored/n_total) ═══")
    header = (
        f"{'pair':<22} | {'variant':<9} | {'n':>3} | "
        f"{'cost ($)':<22} | {'turns':<16} | {'cache_create':<22} | "
        f"{'output':<16} | {'correct':<12} | {'$/correct':<12}"
    )
    print(header)
    print("-" * len(header))

    rows_for_delta: dict[str, dict[str, dict]] = defaultdict(dict)

    for (pair, variant), runs in sorted(groups.items()):
        good = [r for r in runs if _is_run_successful(r)]
        n_total = len(runs)
        n_good = len(good)
        costs = _extract(good, ("total_cost_usd",), 0.0)
        turns = _extract(good, ("num_turns",), 0)
        cache_create = _extract(good, ("usage", "cache_creation_input_tokens"), 0)
        out_tok = _extract(good, ("usage", "output_tokens"), 0)

        rate, n_scored, _ = correctness_rate(good)
        # Cost-per-correct-answer headline metric.
        # Honest definition: total dollars spent / total correct answers.
        # (Earlier draft used median(cost)/rate — close at large n but
        # diverges at small n because the median ignores cost variance
        # across runs. The pure ratio is what a user actually pays per
        # answer they trust.)
        scored_runs = [r for r in good if (r.get("_correctness") or {}).get("passed") in (0, 1)]
        total_cost_scored = sum(r.get("total_cost_usd", 0.0) for r in scored_runs)
        total_correct = sum(1 for r in scored_runs if r["_correctness"]["passed"] == 1)
        if rate is None:
            cpc_str = "—"
            cpc_val: float | None = None
        elif total_correct == 0:
            cpc_str = "∞"
            cpc_val = float("inf")
        else:
            cpc_val = total_cost_scored / total_correct
            cpc_str = f"${cpc_val:.4f}"

        n_label = f"{n_good}"
        if n_total != n_good:
            n_label = f"{n_good}/{n_total}"  # show drop count if any

        print(
            f"{pair:<22} | {variant:<9} | {n_label:>3} | "
            f"{fmt_iqr(costs):<22} | {fmt_iqr(turns, '{:.0f}'):<16} | "
            f"{fmt_iqr(cache_create, '{:.0f}'):<22} | {fmt_iqr(out_tok, '{:.0f}'):<16} | "
            f"{fmt_pct(rate)} ({n_scored}/{n_good}) | {cpc_str:<12}"
        )

        rows_for_delta[pair][variant] = {
            "costs": costs,
            "turns": turns,
            "rate": rate,
            "cpc": cpc_val,
            "n_good": n_good,
        }

    # Per-pair deltas with Mann-Whitney + clearly-better verdict.
    print("\n═══ PER-PAIR DELTAS (with vs without; Mann-Whitney U two-sided) ═══")
    for pair, variants in sorted(rows_for_delta.items()):
        w = variants.get("with")
        wo = variants.get("without")
        if not w or not wo:
            continue
        med_w_cost, med_wo_cost = median(w["costs"]), median(wo["costs"])
        med_w_turns, med_wo_turns = median(w["turns"]), median(wo["turns"])
        leverage = 0.0 if med_wo_cost == 0 else 100 * (med_wo_cost - med_w_cost) / med_wo_cost

        _, p_cost = mann_whitney_u(w["costs"], wo["costs"])
        _, p_turns = mann_whitney_u(w["turns"], wo["turns"])
        clearly_w_cost = clearly_better(w["costs"], wo["costs"])
        clearly_w_turns = clearly_better(w["turns"], wo["turns"])

        # Cost-per-correct delta is the actual decision metric.
        cpc_verdict = "—"
        if w["cpc"] is not None and wo["cpc"] is not None:
            if math.isinf(w["cpc"]) and math.isinf(wo["cpc"]):
                cpc_verdict = "both ∞ (neither arm gets correct answers)"
            elif math.isinf(w["cpc"]):
                cpc_verdict = "WITHOUT wins (with-arm never correct)"
            elif math.isinf(wo["cpc"]):
                cpc_verdict = "WITH wins (without-arm never correct)"
            else:
                if w["cpc"] < wo["cpc"]:
                    delta = 100 * (wo["cpc"] - w["cpc"]) / wo["cpc"]
                    cpc_verdict = f"WITH wins by {delta:+.1f}% on $/correct"
                else:
                    delta = 100 * (w["cpc"] - wo["cpc"]) / wo["cpc"]
                    cpc_verdict = f"WITHOUT wins by {delta:+.1f}% on $/correct"

        print(f"\n{pair}:")
        print(f"  raw cost leverage  : {leverage:+6.1f}%   (median with=${med_w_cost:.4f}  without=${med_wo_cost:.4f})")
        print(f"    Mann-Whitney p   : {p_cost:.4f}  {'clearly-better' if clearly_w_cost else ''}")
        print(f"  turn delta         : with={med_w_turns:.0f}  without={med_wo_turns:.0f}  Mann-Whitney p={p_turns:.4f}  {'clearly-better' if clearly_w_turns else ''}")
        print(f"  correctness        : with={fmt_pct(w['rate'])}  without={fmt_pct(wo['rate'])}")
        print(f"  HEADLINE ($/correct): {cpc_verdict}")


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: summarize_leverage.py <results_dir>\n")
        return 2
    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        sys.stderr.write(f"not a directory: {results_dir}\n")
        return 2
    groups = collect(results_dir)
    if not groups:
        sys.stderr.write(f"no result files found in {results_dir}\n")
        return 2
    print_report(groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
