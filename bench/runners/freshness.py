#!/usr/bin/env python3
"""
Python port of bench/runners/freshness.ts
Operational benchmark suite Bench 4: freshness / supersedes scenarios.
Runs all 10 scenarios and emits JSON summary to bench/results/freshness.json.
"""

import os
import sys
import json
import time
import math
import shutil
from datetime import datetime, timedelta, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from tests.helpers import create_test_vault
from lattice.retrieval.cascade import CascadePipelineFactory
from lattice.retrieval.freshness import ExponentialFreshnessScorer, get_timestamp
from lattice.core.interfaces import Chunk

DAY_MS = 24 * 60 * 60 * 1000

def ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace('+00:00', 'Z')

def rerank_by_freshness(chunks, scorer, since=None):
    now = time.time() * 1000
    since_ts = get_timestamp(since) if since else float('-inf')

    valid_chunks = []
    for c in chunks:
        if c.superseded_by:
            continue
        c_ts = get_timestamp(c.last_seen_at)
        if c_ts >= since_ts:
            valid_chunks.append(c)

    # Sort by score descending
    valid_chunks.sort(key=lambda c: scorer.score(c, now), reverse=True)
    return valid_chunks

# --- Scenario 1 ---
def scenario1():
    tv = create_test_vault()
    try:
        scorer = ExponentialFreshnessScorer()
        c_old = Chunk.from_dict(tv.insert_chunk(
            id="s1-old",
            heading="config baseline",
            body="the team uses 4-space indent",
            source="human_note",
            last_seen_at=ago(30)
        ))
        c_mid = Chunk.from_dict(tv.insert_chunk(
            id="s1-mid",
            heading="config baseline",
            body="the team uses 4-space indent",
            source="human_note",
            last_seen_at=ago(7)
        ))
        c_new = Chunk.from_dict(tv.insert_chunk(
            id="s1-new",
            heading="config baseline",
            body="the team uses 4-space indent",
            source="human_note",
            last_seen_at=ago(1)
        ))
        ranked = rerank_by_freshness([c_old, c_mid, c_new], scorer)
        ids = [c.id for c in ranked]
        expected = ["s1-new", "s1-mid", "s1-old"]
        pass_val = ids == expected
        return {
            "id": 1,
            "description": "decay ordering: T-1d > T-7d > T-30d for same fact",
            "pass": pass_val,
            "expected": " > ".join(expected),
            "actual": " > ".join(ids),
            "reason": None if pass_val else "rank order does not match"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 2 ---
def scenario2():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s2-f1",
            heading="deploy region",
            body="deploy to us-east-1 with t3.medium instances",
            source="human_note",
            last_seen_at=ago(30),
            superseded_by="s2-f2"
        )
        tv.insert_chunk(
            id="s2-f2",
            heading="deploy region",
            body="deploy to us-west-2 with t3.large multi-AZ",
            source="human_note",
            last_seen_at=ago(1),
            supersedes="s2-f1"
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="deploy region", budget_tokens=2500, kind="all")
        ids = [r.chunk.id for r in results]
        pass_val = ("s2-f2" in ids) and ("s2-f1" not in ids)
        return {
            "id": 2,
            "description": "F2 supersedes F1 -> F2 returned, F1 omitted",
            "pass": pass_val,
            "expected": "results contain s2-f2 and NOT s2-f1",
            "actual": f"results = [{', '.join(ids)}]",
            "reason": None if pass_val else "superseded chunk leaked into recall results"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 3 ---
def scenario3():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s3-v1",
            heading="JWT rotation policy",
            body="rotate JWT signing key every 90 days",
            source="human_note",
            superseded_by="s3-v2"
        )
        tv.insert_chunk(
            id="s3-v2",
            heading="JWT rotation policy",
            body="rotate JWT signing key every 60 days",
            source="human_note",
            supersedes="s3-v1",
            superseded_by="s3-v3"
        )
        tv.insert_chunk(
            id="s3-v3",
            heading="JWT rotation policy",
            body="rotate JWT signing key every 30 days with automated ceremony",
            source="human_note",
            supersedes="s3-v2"
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="JWT rotation policy", budget_tokens=2500, kind="all")
        ids = [r.chunk.id for r in results]
        pass_val = ("s3-v3" in ids) and ("s3-v1" not in ids) and ("s3-v2" not in ids)
        return {
            "id": 3,
            "description": "supersedes chain F1->F2->F3 -> only F3 returned",
            "pass": pass_val,
            "expected": "results contain s3-v3 and NOT s3-v1 or s3-v2",
            "actual": f"results = [{', '.join(ids)}]",
            "reason": None if pass_val else "intermediate or original chunk leaked through chain"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 4 ---
def scenario4():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s4-old",
            heading="rate limit policy",
            body="rate limit is 100 requests per minute",
            source="human_note",
            last_seen_at=ago(30)
        )
        tv.insert_chunk(
            id="s4-new",
            heading="rate limit policy",
            body="rate limit is 500 requests per minute",
            source="human_note",
            last_seen_at=ago(1)
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="rate limit policy", budget_tokens=2500, kind="all")
        ids = [r.chunk.id for r in results]
        newer_first = ids.index("s4-new") < ids.index("s4-old") if ("s4-new" in ids and "s4-old" in ids) else False
        pass_val = ("s4-new" in ids) and ("s4-old" in ids) and newer_first
        return {
            "id": 4,
            "description": "conflicting facts, no supersedes -> newer ranks before older",
            "pass": pass_val,
            "expected": "s4-new appears before s4-old in results",
            "actual": f"results = [{', '.join(ids)}]",
            "reason": None if pass_val else "newer chunk did not outrank older when both visible"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 5 ---
def scenario5():
    tv = create_test_vault()
    try:
        scorer = ExponentialFreshnessScorer()
        stale_high = Chunk.from_dict(tv.insert_chunk(
            id="s5-stale-code",
            heading="core API",
            body="function getUser(id) { ... }",
            source="code_index",
            last_seen_at=ago(365)
        ))
        fresh_low = Chunk.from_dict(tv.insert_chunk(
            id="s5-fresh-auto",
            heading="auto-captured note",
            body="user mentioned getUser briefly in chat",
            source="auto_capture",
            last_seen_at=ago(0)
        ))
        now = time.time() * 1000
        stale_high_score = scorer.score(stale_high, now)
        fresh_low_score = scorer.score(fresh_low, now)
        pass_val = stale_high_score > fresh_low_score
        return {
            "id": 5,
            "description": "high source-weight stale beats low source-weight fresh (policy verification)",
            "pass": pass_val,
            "expected": f"code_index@T-365d (={stale_high_score:.3f}) > auto_capture@T-0d (={fresh_low_score:.3f})",
            "actual": f"code_index = {stale_high_score:.3f}, auto_capture = {fresh_low_score:.3f}",
            "notes": "Policy: freshnessScore = SOURCE_WEIGHT[source] x exp(-age/tau[source]). code_index weight=1.0, tau=Infinity. auto_capture weight=0.6, tau=30d.",
            "reason": None if pass_val else "policy expects high-weight stale to win, but it didn't"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 6 ---
def scenario6():
    tv = create_test_vault()
    try:
        scorer = ExponentialFreshnessScorer()
        decision = Chunk.from_dict(tv.insert_chunk(
            id="s6-decision",
            heading="use postgres",
            body="decision: we will use postgres for the primary store",
            source="human_note",
            tags=["decision"],
            last_seen_at=ago(7)
        ))
        observation = Chunk.from_dict(tv.insert_chunk(
            id="s6-observation",
            heading="use postgres",
            body="observation: postgres has been deployed in staging",
            source="human_note",
            tags=["observation"],
            last_seen_at=ago(7)
        ))
        now = time.time() * 1000
        dec_score = scorer.score(decision, now)
        obs_score = scorer.score(observation, now)
        pass_val = abs(dec_score - obs_score) < 1e-9
        return {
            "id": 6,
            "description": "tag-based weighting policy verification",
            "pass": pass_val,
            "expected": "scores equal (no tag weighting in current policy)",
            "actual": f"decision = {dec_score}, observation = {obs_score}, diff = {(dec_score - obs_score):.2e}",
            "notes": "Policy declared: tags are filters/labels only, not score modifiers.",
            "reason": None if pass_val else "scores diverged unexpectedly given current no-tag-weight policy"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 7 ---
def scenario7():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s7-f1",
            heading="auth flow",
            body="auth flow uses session cookies only",
            source="human_note",
            last_seen_at=ago(60),
            superseded_by="s7-f2"
        )
        tv.insert_chunk(
            id="s7-f2",
            heading="auth flow",
            body="auth flow uses session cookies and CSRF tokens",
            source="human_note",
            last_seen_at=ago(30),
            supersedes="s7-f1",
            superseded_by="s7-f3"
        )
        tv.insert_chunk(
            id="s7-f3",
            heading="auth flow",
            body="auth flow uses JWT bearer tokens with refresh rotation",
            source="human_note",
            last_seen_at=ago(1),
            supersedes="s7-f2"
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="auth flow", budget_tokens=2500, kind="all")
        ids = [r.chunk.id for r in results]
        pass_val = ("s7-f3" in ids) and ("s7-f1" not in ids) and ("s7-f2" not in ids)
        return {
            "id": 7,
            "description": "revoke chain: F1 still suppressed when F3 supersedes F2",
            "pass": pass_val,
            "expected": "results contain s7-f3, exclude s7-f1 and s7-f2",
            "actual": f"results = [{', '.join(ids)}]",
            "reason": None if pass_val else "F1 or F2 leaked through despite each having superseded_by set"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 8 ---
def scenario8():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s8-f1",
            heading="cors policy",
            body="cors policy allows only same-origin requests",
            source="human_note",
            path="/repo/a/cors.md",
            last_seen_at=ago(30),
            superseded_by="s8-f2"
        )
        tv.insert_chunk(
            id="s8-f2",
            heading="cors policy",
            body="cors policy allows configured origins via env var",
            source="human_note",
            path="/repo/b/cors.md",
            last_seen_at=ago(1),
            supersedes="s8-f1"
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="cors policy", budget_tokens=2500, kind="all", path_scope="/repo/a")
        ids = [r.chunk.id for r in results]
        pass_val = "s8-f1" not in ids
        return {
            "id": 8,
            "description": "cross-path supersedes: path scope does not unhide superseded chunk",
            "pass": pass_val,
            "expected": "s8-f1 NOT in results (even when scoped to its own path)",
            "actual": f"results = [{', '.join(ids)}]",
            "reason": None if pass_val else "stale chunk leaked through because path-scoped query bypassed the supersedes filter"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 9 ---
def scenario9():
    tv = create_test_vault()
    try:
        tv.insert_chunk(
            id="s9-ancient",
            heading="legacy db schema",
            body="the legacy db schema uses a single users table with a json blob",
            source="human_note",
            last_seen_at=ago(365)
        )
        pipeline = CascadePipelineFactory.create(tv.db)
        results = pipeline.retrieve(query="legacy db schema", budget_tokens=2500, kind="all")
        ids = [r.chunk.id for r in results]
        pass_val = "s9-ancient" in ids
        score = next((r.freshness for r in results if r.chunk.id == "s9-ancient"), None) if pass_val else None
        return {
            "id": 9,
            "description": "decay floor: T-1y fact retrievable when no fresher equivalent exists",
            "pass": pass_val,
            "expected": "s9-ancient appears in results with low freshness score (~0.1)",
            "actual": f"results = [{', '.join(ids)}], freshness = {('n/a' if score is None else f'{score:.3f}')}",
            "reason": None if pass_val else "very old chunk was filtered out instead of being returned with low rank"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

# --- Scenario 10 ---
def scenario10():
    tv = create_test_vault()
    try:
        scorer = ExponentialFreshnessScorer()
        pinned_stale = Chunk.from_dict(tv.insert_chunk(
            id="s10-pinned-old",
            heading="team convention",
            body="indent with 4 spaces in python",
            source="human_note",
            last_seen_at=ago(90),
            pinned=1
        ))
        fresh_unpinned = Chunk.from_dict(tv.insert_chunk(
            id="s10-fresh-unpinned",
            heading="team convention",
            body="indent with 2 spaces in python",
            source="human_note",
            last_seen_at=ago(0)
        ))
        ranked = rerank_by_freshness([fresh_unpinned, pinned_stale], scorer)
        pass_val = ranked[0].id == pinned_stale.id
        return {
            "id": 10,
            "description": "pinned fact at T-90d outranks fresh unpinned",
            "pass": pass_val,
            "expected": "pinned chunk ranks first regardless of age",
            "actual": "pinned T-90d ranked first; fresh unpinned ranked second" if pass_val else f"top was {ranked[0].id}, expected {pinned_stale.id}",
            "reason": None if pass_val else "pinned chunk did not win freshness ranking"
        }
    finally:
        tv.close()
        shutil.rmtree(tv.tmp_dir, ignore_errors=True)

SCENARIOS = [
    scenario1,
    scenario2,
    scenario3,
    scenario4,
    scenario5,
    scenario6,
    scenario7,
    scenario8,
    scenario9,
    scenario10
]

def main():
    started_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    scenarios_results = []
    crash_count = 0

    for scenario in SCENARIOS:
        try:
            res = scenario()
            scenarios_results.append(res)
            symbol = "✓" if res["pass"] else "✗"
            print(f"{symbol} #{res['id']}: {res['description']}", file=sys.stderr)
            if not res["pass"]:
                print(f"    reason: {res.get('reason') or '—'}", file=sys.stderr)
        except Exception as e:
            crash_count += 1
            import traceback
            msg = traceback.format_exc()
            scenarios_results.append({
                "id": len(scenarios_results) + 1,
                "description": "(crashed before producing a result)",
                "pass": False,
                "expected": "scenario completes",
                "actual": "scenario threw",
                "reason": f"crash: {msg[:500]}"
            })
            print(f"✗ scenario crashed: {str(e)[:200]}", file=sys.stderr)

    pass_count = len([s for s in scenarios_results if s["pass"]])
    total = len(scenarios_results)

    result = {
        "meta": {
            "date": started_at,
            "node": sys.version,
            "platform": sys.platform
        },
        "results": {
            "scenarios": scenarios_results,
            "pass_count": pass_count,
            "total": total
        },
        "summary": {
            "verdict": f"all {total} scenarios pass — freshness/supersedes invariants hold" if pass_count == total else f"{pass_count}/{total} pass; {total - pass_count} failures named below",
            "failures": [{"id": s["id"], "description": s["description"], "reason": s["reason"]} for s in scenarios_results if not s["pass"]]
        }
    }

    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../results'))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "freshness.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    print(f"\n→ wrote {os.path.relpath(out_path, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))}", file=sys.stderr)
    print(f"→ {result['summary']['verdict']}", file=sys.stderr)

    if crash_count > 0:
        sys.exit(2)

if __name__ == '__main__':
    main()
