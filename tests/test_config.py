import pytest
from lattice.core.config import LatticeConfig, get_config, reset_config


@pytest.fixture(autouse=True)
def clean_config():
    """Reset singleton between tests."""
    reset_config()
    yield
    reset_config()


class TestLatticeConfig:
    def test_defaults(self):
        config = LatticeConfig()
        assert config.vault_dir == '.lattice'
        assert config.embeddings_enabled is False
        assert config.reranker_enabled is False
        assert config.rrf_k == 60
        assert config.retention_days == 365
        assert config.autosummary is True
        assert config.block_min_chars == 40000

    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv('LATTICE_VAULT_DIR', '/custom/path')
        monkeypatch.setenv('LATTICE_EMBEDDINGS', 'on')
        monkeypatch.setenv('LATTICE_RERANKER', 'on')
        monkeypatch.setenv('LATTICE_RRF_K', '30')
        monkeypatch.setenv('LATTICE_RETENTION_DAYS', '90')
        monkeypatch.setenv('LATTICE_AUTOSUMMARY', 'off')

        config = LatticeConfig()
        assert config.vault_dir == '/custom/path'
        assert config.embeddings_enabled is True
        assert config.reranker_enabled is True
        assert config.rrf_k == 30
        assert config.retention_days == 90
        assert config.autosummary is False

    def test_frozen(self):
        config = LatticeConfig()
        with pytest.raises(Exception):
            config.rrf_k = 999


class TestGetConfig:
    def test_returns_same_instance(self):
        a = get_config()
        b = get_config()
        assert a is b

    def test_reset_clears_singleton(self, monkeypatch):
        _ = get_config()
        monkeypatch.setenv('LATTICE_RRF_K', '42')
        reset_config()
        assert get_config().rrf_k == 42

    def test_respects_env_at_creation_time(self, monkeypatch):
        monkeypatch.setenv('LATTICE_EMBEDDINGS', 'on')
        reset_config()
        assert get_config().embeddings_enabled is True
