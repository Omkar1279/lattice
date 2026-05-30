import os
import pytest
from unittest.mock import patch, MagicMock
from helpers import create_test_vault
from lattice.retrieval.hipporag import hipporag_retrieve

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def test_hipporag_pagerank(vault):
    # Setup node and edges in database
    db = vault.vault.db
    
    # Insert chunks
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c1', 'h1', 'body1', 'code_index', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c2', 'h2', 'body2', 'code_index', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c3', 'h3', 'body3', 'code_index', 'p1', 'now', 'now', 'now')")
    
    # Insert FTS index entries for chunks
    db.execute("INSERT INTO chunks_fts (rowid, heading, body) VALUES (1, 'h1', 'body1')")
    db.execute("INSERT INTO chunks_fts (rowid, heading, body) VALUES (2, 'h2', 'body2')")
    
    # Insert edges
    db.execute("INSERT INTO edges (source_chunk_id, target_chunk_id, kind) VALUES ('c1', 'c2', 'imports')")
    db.execute("INSERT INTO edges (source_chunk_id, target_chunk_id, kind) VALUES ('c2', 'c3', 'calls')")
    
    # Mock BM25 Search Strategy to return seeds
    class FakeBM25:
        def __init__(self, db): pass
        def search(self, query, limit):
            from lattice.core.interfaces import Chunk
            return [
                Chunk(id='c1', heading='h1', body='body1', source='code_index', path='p1')
            ]
            
    with patch("lattice.retrieval.hipporag.BM25SearchStrategy", FakeBM25):
        hits = hipporag_retrieve(db, "test query", limit=3)
        assert len(hits) > 0
        # HippoRAG should traverse edges and return c1, c2, c3
        hit_ids = [h.id for h in hits]
        assert "c1" in hit_ids
        assert "c2" in hit_ids
        assert "c3" in hit_ids

def test_resolve_and_write_edges_integration(vault):
    db = vault.vault.db
    # Seed source and target chunks
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('src_chunk', 'a.py', 'import b', 'code_index', '/project/a.py', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('tgt_chunk', 'b.py', 'def helper(): pass', 'code_index', '/project/b.py', 'now', 'now', 'now')")
    
    # Resolve the import edge
    from lattice.indexer.graph import resolve_and_write_edges
    raw_edges = [
        {"kind": "imports", "target_symbol": "./b", "line": 0, "confidence": 1.0}
    ]
    resolve_and_write_edges(vault.vault, "src_chunk", "/project/a.py", raw_edges, "/project")
    
    # Query edges table
    row = db.execute("SELECT * FROM edges WHERE source_chunk_id = ?", ("src_chunk",)).fetchone()
    assert row is not None
    assert row["target_chunk_id"] == "tgt_chunk"
    assert row["kind"] == "imports"
    assert row["confidence"] == 1.0
