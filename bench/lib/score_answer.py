"""Grade a single leverage-bench run against the fixture's oracle.

Usage:
    python3 -m bench.lib.score_answer <oracle.yaml> <pair_key> <result.json>

Reads the model's final answer (`.result` in result.json), looks up the
oracle for `pair_key` in the YAML, runs the appropriate grader, and writes
back into result.json under the `_correctness` key (schema-additive — no
existing fields touched).

Two grader types:

  - objective : substring/regex match on the answer text.
      Required keys:
        must_contain_paths  (list[str], all required)   OR
        must_contain_terms  (list[str], all required)
      Optional:
        must_not_contain_terms (list[str], none may appear)
        must_contain_any_of (list[list[str]], each inner list = at-least-one)

  - narrative : invokes Opus 4.7 as judge with judge_question. Self-preference
      bias is mitigated by judge-stronger-than-judged (Opus 4.7 > Sonnet 4.6
      under test). Judge is prompted to answer strictly YES or NO.
      Required keys:
        judge_question (str)
      Optional belt-and-suspenders:
        must_contain_terms (list[str]) — short-circuit FAIL if missing

Exits 0 on successful scoring (regardless of pass/fail). Exits 2 on
oracle/data errors so the harness can flag them.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write("score_answer.py: PyYAML required (uv pip install pyyaml)\n")
    sys.exit(2)


JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-opus-4-7")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _all_present(needles: list[str], haystack: str) -> tuple[bool, list[str]]:
    missing = [n for n in needles if n not in haystack]
    return (len(missing) == 0, missing)


def _any_groups_present(groups: list[list[str]], haystack: str) -> tuple[bool, list[list[str]]]:
    """For each inner list, at least one member must appear. Returns ok + groups that failed."""
    failed = [g for g in groups if not any(item in haystack for item in g)]
    return (len(failed) == 0, failed)


def grade_objective(answer: str, oracle: dict) -> dict:
    details: dict[str, Any] = {}
    failures: list[str] = []

    paths = oracle.get("must_contain_paths") or []
    if paths:
        ok, missing = _all_present(paths, answer)
        details["paths_required"] = paths
        details["paths_missing"] = missing
        if not ok:
            failures.append(f"missing required paths: {missing}")

    terms = oracle.get("must_contain_terms") or []
    if terms:
        ok, missing = _all_present(terms, answer)
        details["terms_required"] = terms
        details["terms_missing"] = missing
        if not ok:
            failures.append(f"missing required terms: {missing}")

    forbidden = oracle.get("must_not_contain_terms") or []
    if forbidden:
        present = [t for t in forbidden if t in answer]
        details["forbidden_terms_present"] = present
        if present:
            failures.append(f"forbidden terms present: {present}")

    any_groups = oracle.get("must_contain_any_of") or []
    if any_groups:
        ok, failed_groups = _any_groups_present(any_groups, answer)
        details["any_groups_required"] = any_groups
        details["any_groups_failed"] = failed_groups
        if not ok:
            failures.append(f"any-of groups failed: {failed_groups}")

    if not (paths or terms or any_groups):
        # Oracle is empty — treat as unscorable rather than auto-pass.
        return {
            "passed": None,
            "method": "objective",
            "details": {"error": "oracle has no must_contain_* keys"},
        }

    return {
        "passed": 1 if not failures else 0,
        "method": "objective",
        "details": details,
        "failures": failures,
    }


def grade_narrative(answer: str, oracle: dict) -> dict:
    # Belt-and-suspenders short-circuit: if must_contain_terms is set and
    # missing, fail without spending a judge call.
    must_terms = oracle.get("must_contain_terms") or []
    if must_terms:
        ok, missing = _all_present(must_terms, answer)
        if not ok:
            return {
                "passed": 0,
                "method": "narrative",
                "details": {"short_circuit": f"missing required terms: {missing}"},
                "judge_called": False,
            }

    judge_question = oracle.get("judge_question")
    if not judge_question:
        return {
            "passed": None,
            "method": "narrative",
            "details": {"error": "narrative oracle missing judge_question"},
        }

    judge_prompt = (
        "You are an impartial grader. Read the candidate answer below and "
        "answer the grading question with EXACTLY one word: YES or NO. "
        "Do not explain. Do not hedge. Just YES or NO.\n\n"
        f"GRADING QUESTION:\n{judge_question}\n\n"
        f"CANDIDATE ANSWER:\n{answer}\n\n"
        "Your one-word verdict (YES or NO):"
    )

    try:
        completed = subprocess.run(
            [
                "claude", "-p",
                "--model", JUDGE_MODEL,
                "--output-format", "json",
                "--max-budget-usd", "0.10",
                "--no-session-persistence",
                judge_prompt,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {
            "passed": None,
            "method": "narrative",
            "details": {"error": f"judge call failed: {e}"},
            "judge_called": True,
        }

    if completed.returncode != 0:
        return {
            "passed": None,
            "method": "narrative",
            "details": {"error": f"judge exit={completed.returncode}", "stderr": completed.stderr[:500]},
            "judge_called": True,
        }

    try:
        judge_data = json.loads(completed.stdout)
        verdict_raw = (judge_data.get("result") or "").strip().upper()
    except json.JSONDecodeError:
        verdict_raw = completed.stdout.strip().upper()

    # Tolerant parse: accept "YES", "YES.", "**YES**", trailing junk, etc.
    m = re.search(r"\b(YES|NO)\b", verdict_raw)
    verdict = m.group(1) if m else None

    return {
        "passed": 1 if verdict == "YES" else (0 if verdict == "NO" else None),
        "method": "narrative",
        "details": {
            "judge_model": JUDGE_MODEL,
            "judge_verdict_raw": verdict_raw[:200],
            "judge_verdict_parsed": verdict,
        },
        "judge_called": True,
    }


def main() -> int:
    if len(sys.argv) != 4:
        sys.stderr.write(__doc__ or "")
        return 2

    oracle_path, pair_key, result_path = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        with open(oracle_path, "r", encoding="utf-8") as f:
            oracle_doc = yaml.safe_load(f)
    except FileNotFoundError:
        # No oracle for this fixture — record as unscored, exit clean.
        _write_unscored(result_path, reason=f"no oracle file at {oracle_path}")
        return 0

    pair_oracle = (oracle_doc.get("pairs") or {}).get(pair_key)
    if not pair_oracle:
        _write_unscored(result_path, reason=f"no oracle for pair {pair_key}")
        return 0

    oracle = pair_oracle.get("oracle") or {}

    # Stub detection: refuse to score if the oracle still contains TODO markers
    # (e.g. uniacco-site placeholder file the user hasn't filled in).
    if "TODO" in json.dumps(oracle):
        _write_unscored(result_path, reason="oracle contains TODO placeholders")
        return 0

    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        sys.stderr.write(f"score_answer: cannot read result {result_path}: {e}\n")
        return 2

    answer = result.get("result") or ""
    if not answer:
        _write_unscored(result_path, reason="result.json has empty .result")
        return 0

    otype = oracle.get("type")
    if otype == "objective":
        scored = grade_objective(answer, oracle)
    elif otype == "narrative":
        scored = grade_narrative(answer, oracle)
    else:
        _write_unscored(result_path, reason=f"unknown oracle type: {otype}")
        return 0

    scored["scored_at"] = _now_iso()
    scored["pair_key"] = pair_key
    result["_correctness"] = scored

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return 0


def _write_unscored(result_path: str, reason: str) -> None:
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    result["_correctness"] = {
        "passed": None,
        "method": "unscored",
        "details": {"reason": reason},
        "scored_at": _now_iso(),
    }
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    sys.exit(main())
