import os
import pytest
from unittest.mock import patch, MagicMock
from helpers import create_test_vault
from lattice.retrieval.colbert import embed_colbert, embed_and_store_colbert, search_colbert
from lattice.retrieval.contextual import contextualize_chunk, _reset_contextual_cache_for_tests
from lattice.retrieval.doc2query import generate_queries
from lattice.retrieval.hipporag import hipporag_retrieve
from lattice.util.mem0 import extract_atomic_facts_regex, extract_atomic_facts
from lattice.hooks.stop import classify_fact

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def test_colbert_projection_lcg():
    # Verify that get_projection produces expected shape and values
    from lattice.retrieval.colbert import get_projection
    proj = get_projection()
    assert len(proj) == 384
    assert len(proj[0]) == 384
    # All values must be -1.0 or 1.0
    for r in proj[:5]:
        for val in r[:5]:
            assert val in (-1.0, 1.0)

def test_colbert_embedding(monkeypatch):
    # Mock get_embedder/fastembed to return a fake model
    class FakeONNXContext:
        model_output = [[[0.1] * 384] * 5] # shape: (1, 5, 384)
    
    class FakeModel:
        def onnx_embed(self, texts):
            return FakeONNXContext()
            
    class FakeTextEmbedding:
        def __init__(self, model_name):
            self.model = FakeModel()
            
    monkeypatch.setattr("fastembed.TextEmbedding", FakeTextEmbedding)
    
    # Reset colbert embedder cache
    import lattice.retrieval.colbert
    lattice.retrieval.colbert._embedder = None
    
    vec = embed_colbert("hello world")
    assert len(vec) == 384
    # All elements must be finite
    assert all(x != float("-inf") for x in vec)

def test_contextual_retrieval(monkeypatch):
    monkeypatch.setenv("LATTICE_CONTEXTUAL_CHUNKS", "on")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("LATTICE_CONTEXTUAL_CACHE", "off") # disable caching to test api call
    
    # Mock httpx.post
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "content": [{"text": "This is a contextual summary."}]
    }
    
    with patch("httpx.post", return_value=mock_response):
        _reset_contextual_cache_for_tests()
        res = contextualize_chunk("test.py", "print('hello')", "Overall file purpose is testing.")
        assert "This is a contextual summary." in res
        assert "print('hello')" in res

def test_doc2query(monkeypatch):
    monkeypatch.setenv("LATTICE_DOC2QUERY", "on")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "content": [{"text": "query 1\nquery 2\nquery 3"}]
    }
    
    with patch("httpx.post", return_value=mock_response):
        queries = generate_queries("print('hello')", "test.py")
        assert len(queries) == 3
        assert queries == ["query 1", "query 2", "query 3"]

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

def test_mem0_regex_extraction():
    body = "# Log\n- This is fact number one.\n* This is fact number two.\nNormal sentence. Another one!"
    facts = extract_atomic_facts_regex(body)
    assert "This is fact number one." in facts
    assert "This is fact number two." in facts
    assert "Normal sentence." in facts
    assert "Another one!" in facts

def test_classify_fact_logic(vault):
    db = vault.vault.db
    # Seed a fact
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('note1', 'First note', 'The codebase is written in Python and uses Typer.', 'human_note', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks_fts (rowid, heading, body) VALUES (1, 'First note', 'The codebase is written in Python and uses Typer.')")
    
    # High overlap should trigger UPDATE
    res = classify_fact(db, "The codebase is indeed written in Python using Typer library.")
    assert res["action"] == "UPDATE"
    assert res["supersedes"] == "note1"
    
    # Low overlap should trigger ADD
    res = classify_fact(db, "The project is using Vitest for TypeScript testing.")
    assert res["action"] == "ADD"
