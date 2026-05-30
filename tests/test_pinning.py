import pytest
import time
from datetime import datetime
from lattice.retrieval.freshness import ExponentialFreshnessScorer
from lattice.core.interfaces import Chunk

DAY_MS = 24 * 60 * 60 * 1000

_scorer = ExponentialFreshnessScorer()

def make_chunk(overrides=None):
    now = datetime.now().isoformat()
    base = {
        'id': 'x',
        'heading': 'h',
        'body': 'b',
        'source': 'human_note',
        'path': '/x',
        'tags': [],
        'created_at': now,
        'last_seen_at': now,
        'last_validated_at': now,
        'pinned': 0
    }
    if overrides:
        base.update(overrides)
    return base

def test_pinning_invariant_pinned_outranks_fresh_unpinned():
    now_ts = time.time() * 1000
    past_date = datetime.fromtimestamp((now_ts - 90 * DAY_MS) / 1000).isoformat()
    now_date = datetime.fromtimestamp(now_ts / 1000).isoformat()
    
    pinned_stale = make_chunk({
        'pinned': 1,
        'last_seen_at': past_date
    })
    fresh_unpinned = make_chunk({
        'pinned': 0,
        'last_seen_at': now_date
    })
    
    assert _scorer.score(Chunk.from_dict(pinned_stale), now_ts) > _scorer.score(Chunk.from_dict(fresh_unpinned), now_ts)

def test_pinning_unpinned_chunks_fallback_to_normal_decay():
    now_ts = time.time() * 1000
    fresh = make_chunk({
        'pinned': 0,
        'last_seen_at': datetime.fromtimestamp(now_ts / 1000).isoformat()
    })
    
    score = _scorer.score(Chunk.from_dict(fresh), now_ts)
    assert abs(score - 0.9) < 0.01
