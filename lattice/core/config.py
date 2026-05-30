"""Centralized configuration using Config-Driven Pattern.

All environment variables and defaults are read once here. Other modules
depend on this config object rather than scattering os.environ reads.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LatticeConfig:
    """Immutable application configuration (read once at startup)."""

    vault_dir: str = field(default_factory=lambda: os.environ.get('LATTICE_VAULT_DIR', '.lattice'))
    embeddings_enabled: bool = field(default_factory=lambda: os.environ.get('LATTICE_EMBEDDINGS') == 'on')
    reranker_enabled: bool = field(default_factory=lambda: os.environ.get('LATTICE_RERANKER') == 'on')
    rrf_k: int = field(default_factory=lambda: int(os.environ.get('LATTICE_RRF_K', '60')))
    port: int = field(default_factory=lambda: int(os.environ.get('LATTICE_PORT', str(37700 + (os.getuid() % 100)))))
    block_min_chars: int = field(default_factory=lambda: int(os.environ.get('LATTICE_BLOCK_MIN_CHARS', '40000')))
    retention_days: int = field(default_factory=lambda: int(os.environ.get('LATTICE_RETENTION_DAYS', '365')))
    autosummary: bool = field(default_factory=lambda: os.environ.get('LATTICE_AUTOSUMMARY') != 'off')
    embed_model: str = field(default_factory=lambda: os.environ.get('LATTICE_EMBED_MODEL', 'BAAI/bge-small-en-v1.5'))
    rerank_model: str = field(default_factory=lambda: os.environ.get('LATTICE_RERANK_MODEL', 'mixedbread-ai/mxbai-rerank-xsmall-v2'))


# Module-level singleton — instantiated once on first import
_config: LatticeConfig | None = None


def get_config() -> LatticeConfig:
    """Get the global config (lazy singleton)."""
    global _config
    if _config is None:
        _config = LatticeConfig()
    return _config


def reset_config() -> None:
    """Reset config (for testing with modified env vars)."""
    global _config
    _config = None
