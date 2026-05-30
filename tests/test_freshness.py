import pytest
import time
from datetime import datetime, timedelta
from lattice.retrieval.freshness import ExponentialFreshnessScorer
from lattice.core.interfaces import Chunk

DAY_MS = 24 * 60 * 60 * 1000

_scorer = ExponentialFreshnessScorer()

def make_chunk(overrides=None):
    now = datetime.now().isoformat()
    base = {
        'id': f"test-{int(time.time() * 1000)}",
        'heading': 'test chunk',
        'body': 'test body',
        'source': 'human_note',
        'path': '/test/file.md',
        'tags': [],
        'created_at': now,
        'last_seen_at': now,
        'last_validated_at': now,
        'pinned': 0
    }
    if overrides:
        base.update(overrides)
    return base

def test_freshness_code_index_has_score_1_regardless_of_age():
    now_ts = time.time() * 1000
    old_date = datetime.fromtimestamp((now_ts - 365 * DAY_MS) / 1000).isoformat()
    old_chunk = make_chunk({
        'source': 'code_index',
        'last_seen_at': old_date
    })
    
    assert _scorer.score(Chunk.from_dict(old_chunk), now_ts) == 1.0

def test_freshness_human_note_decays_over_time_with_tau_180d():
    now_ts = time.time() * 1000
    now_date = datetime.fromtimestamp(now_ts / 1000).isoformat()
    stale_date = datetime.fromtimestamp((now_ts - 180 * DAY_MS) / 1000).isoformat()
    
    fresh = make_chunk({
        'source': 'human_note',
        'last_seen_at': now_date
    })
    stale = make_chunk({
        'source': 'human_note',
        'last_seen_at': stale_date
    })
    
    fresh_score = _scorer.score(Chunk.from_dict(fresh), now_ts)
    stale_score = _scorer.score(Chunk.from_dict(stale), now_ts)
    
    assert fresh_score > stale_score
    import math
    assert abs(fresh_score - 0.9) < 0.01
    assert abs(stale_score - (0.9 * math.exp(-1))) < 0.01

def test_freshness_auto_capture_decays_faster_than_human_note():
    now_ts = time.time() * 1000
    age = 60 * DAY_MS
    past_date = datetime.fromtimestamp((now_ts - age) / 1000).isoformat()
    
    human_note = make_chunk({
        'source': 'human_note',
        'last_seen_at': past_date
    })
    auto_capture = make_chunk({
        'source': 'auto_capture',
        'last_seen_at': past_date
    })
    
    human_score = _scorer.score(Chunk.from_dict(human_note), now_ts)
    auto_score = _scorer.score(Chunk.from_dict(auto_capture), now_ts)
    
    assert human_score > auto_score

def test_freshness_fresh_auto_capture_can_outrank_very_old_human_note():
    now_ts = time.time() * 1000
    now_date = datetime.fromtimestamp(now_ts / 1000).isoformat()
    past_date = datetime.fromtimestamp((now_ts - 360 * DAY_MS) / 1000).isoformat()
    
    fresh_auto = make_chunk({
        'source': 'auto_capture',
        'last_seen_at': now_date
    })
    very_old_human = make_chunk({
        'source': 'human_note',
        'last_seen_at': past_date
    })
    
    auto_score = _scorer.score(Chunk.from_dict(fresh_auto), now_ts)
    human_score = _scorer.score(Chunk.from_dict(very_old_human), now_ts)
    
    assert auto_score > human_score
