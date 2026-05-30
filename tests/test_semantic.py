import pytest
import sqlite3
from unittest.mock import patch, MagicMock
from helpers import create_test_vault
from lattice.retrieval.semantic import SemanticSearchStrategy, NullEmbedder


def vec0_available():
    import sqlite_vec
    db = sqlite3.connect(':memory:')
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.execute('CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, e float[8])')
        return True
    except Exception:
        return False

pytestmark = pytest.mark.skipif(not vec0_available(), reason="vec0 not available")

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()


class FakeEmbedder(NullEmbedder):
    def embed(self, text: str):
        return [0.1] * 384


def test_semantic_first_insert_lands_row(vault, monkeypatch):
    monkeypatch.setenv('LATTICE_EMBEDDINGS', 'on')
    strategy = SemanticSearchStrategy(vault.vault.db, embedder=FakeEmbedder())
    strategy.embed_and_store('chunk-a', 'hello world')
    
    row = vault.db.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()
    assert row['n'] == 1
    
    row = vault.db.execute("SELECT chunk_id FROM chunks_vec WHERE chunk_id = ?", ('chunk-a',)).fetchone()
    assert row['chunk_id'] == 'chunk-a'


def test_semantic_second_insert_replaces_in_place(vault, monkeypatch):
    monkeypatch.setenv('LATTICE_EMBEDDINGS', 'on')
    strategy = SemanticSearchStrategy(vault.vault.db, embedder=FakeEmbedder())
    strategy.embed_and_store('chunk-a', 'first content')
    strategy.embed_and_store('chunk-a', 'second content')
    
    row = vault.db.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()
    assert row['n'] == 1

    
def test_semantic_distinct_ids_coexist(vault, monkeypatch):
    monkeypatch.setenv('LATTICE_EMBEDDINGS', 'on')
    strategy = SemanticSearchStrategy(vault.vault.db, embedder=FakeEmbedder())
    strategy.embed_and_store('chunk-a', 'alpha')
    strategy.embed_and_store('chunk-b', 'beta')
    
    row = vault.db.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()
    assert row['n'] == 2
