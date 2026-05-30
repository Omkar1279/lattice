"""Freshness scoring — config-driven exponential decay.

New sources are added by extending the SOURCES config dict.
No subclassing needed for simple decay parameter changes.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict

from lattice.core.interfaces import Chunk, FreshnessScorer

DAY_MS = 24 * 60 * 60 * 1000
PINNED_SCORE = 1e6


def get_timestamp(iso_str: str) -> float:
    """Robust ISO 8601 to timestamp (ms) conversion."""
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str.replace('Z', '+00:00')).timestamp() * 1000
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class SourceConfig:
    """Config for a single source type: weight and decay tau in days."""

    weight: float
    tau_days: float  # float('inf') means no decay


# Default source configuration (OCP: extend by adding entries, not modifying code)
DEFAULT_SOURCE_CONFIGS: Dict[str, SourceConfig] = {
    'code_index': SourceConfig(weight=1.0, tau_days=float('inf')),
    'human_note': SourceConfig(weight=0.9, tau_days=180.0),
    'auto_capture': SourceConfig(weight=0.6, tau_days=30.0),
}

_FALLBACK = SourceConfig(weight=0.6, tau_days=30.0)


class ExponentialFreshnessScorer(FreshnessScorer):
    """Config-driven freshness scorer with exponential decay per source."""

    def __init__(self, configs: Dict[str, SourceConfig] | None = None):
        self._configs = configs if configs is not None else DEFAULT_SOURCE_CONFIGS

    def score(self, chunk: Chunk, now_ms: float) -> float:
        if chunk.pinned:
            return PINNED_SCORE

        age_days = max(0.0, (now_ms - get_timestamp(chunk.last_seen_at)) / DAY_MS)
        cfg = self._configs.get(chunk.source, _FALLBACK)

        decay = 1.0 if cfg.tau_days == float('inf') else math.exp(-age_days / cfg.tau_days)
        return decay * cfg.weight
