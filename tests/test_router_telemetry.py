import os
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from helpers import create_test_vault
from lattice.storage.vault import open_vault
from lattice.hooks.session_start import handle_session_start
from lattice.daemon import recall_expand

def test_session_start_hook_without_skill(monkeypatch):
    # Test session start without SKILL.md
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        monkeypatch.setattr(os, "getcwd", lambda: tmp_dir)
        
        # Test it returns the default payload message
        out = handle_session_start("")
        assert "[lattice] Token-saving memory active." in out
        assert "SKILL.md" not in out

def test_session_start_hook_with_skill(monkeypatch):
    # Test session start with a temporary SKILL.md in getcwd()
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        monkeypatch.setattr(os, "getcwd", lambda: tmp_dir)
        
        skill_file = Path(tmp_dir) / "SKILL.md"
        skill_content = "---\nname: lattice-retrieval\ndescription: Test retrieval plugin\n---\n# Test Guidance"
        skill_file.write_text(skill_content, encoding="utf-8")
        
        out = handle_session_start("")
        assert "[lattice] Token-saving memory active." in out
        assert "Test Guidance" in out
        assert "Test retrieval plugin" in out

def test_session_start_hook_with_skill_truncation(monkeypatch):
    # Test session start hook with a very long SKILL.md exceeding the budget
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        monkeypatch.setattr(os, "getcwd", lambda: tmp_dir)
        
        skill_file = Path(tmp_dir) / "SKILL.md"
        # Make a long skill prompt exceeding the 2000 token limit
        long_content = "extremely long string " * 500
        skill_file.write_text(long_content, encoding="utf-8")
        
        out = handle_session_start("")
        # The result must be within 2000 tokens
        from lattice.util.tokens import count_tokens
        assert count_tokens(out) <= 2000

def test_recall_expand_telemetry_logging(monkeypatch):
    # Test that recall_expand logs the mode to telemetry.log
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        
        # Mock vault and tool execution to avoid DB dependencies failing
        mock_vault = MagicMock()
        mock_vault.db = MagicMock()
        mock_vault.close = MagicMock()
        
    with patch("lattice.storage.open_vault", return_value=mock_vault):
        with patch("lattice.tools.handle_recall_expand", return_value="expanded"):
            # Call recall_expand
            recall_expand(chunk_id="c1", mode="callers")
            
            # Check that telemetry log contains the record
            log_file = Path(tmp_dir) / "log" / "telemetry.log"
            assert log_file.exists()
            log_content = log_file.read_text(encoding="utf-8")
            assert "recall_expand:callers\n" in log_content

            # Call with another mode
            recall_expand(chunk_id="c1", mode="imports")
            log_content = log_file.read_text(encoding="utf-8")
            assert "recall_expand:imports\n" in log_content

def test_weighted_hipporag_transition():
    # Test that hipporag_retrieve uses edge weights properly
    vault = create_test_vault()
    db = vault.db
    
    # Create chunks
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c1', 'h1', 'body1', 'code_index', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c2', 'h2', 'body2', 'code_index', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c3', 'h3', 'body3', 'code_index', 'p1', 'now', 'now', 'now')")
    db.execute("INSERT INTO chunks_fts (rowid, heading, body) VALUES (1, 'h1', 'body1')")
    
    # c1 is seed
    # c1 -> c2 with calls (weight 1.0)
    # c1 -> c3 with references (weight 0.5)
    db.execute("INSERT INTO edges (source_chunk_id, target_chunk_id, kind, confidence) VALUES ('c1', 'c2', 'calls', 1.0)")
    db.execute("INSERT INTO edges (source_chunk_id, target_chunk_id, kind, confidence) VALUES ('c1', 'c3', 'references', 1.0)")
    
    class FakeBM25:
        def __init__(self, db): pass
        def search(self, query, limit):
            from lattice.core.interfaces import Chunk
            return [Chunk(id='c1', heading='h1', body='body1', source='code_index', path='p1')]
            
    with patch("lattice.retrieval.hipporag.BM25SearchStrategy", FakeBM25):
        from lattice.retrieval.hipporag import hipporag_retrieve
        hits = hipporag_retrieve(db, "test query", limit=3)
        # c2 should receive more score than c3 because calls (1.0) > references (0.5)
        # So c2 should come before c3 in the retrieved list!
        hit_ids = [h.id for h in hits]
        assert "c2" in hit_ids
        assert "c3" in hit_ids
        assert hit_ids.index("c2") < hit_ids.index("c3")
    vault.close()

def test_quoted_result_tracking_post_tool_use(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        
        # Setup session_recalled.json
        session_file = Path(tmp_dir) / 'session_recalled.json'
        session_file.write_text(json.dumps([
            {'id': 'chunk123', 'heading': 'Core Auth Logic'}
        ]), encoding='utf-8')
        
        from lattice.hooks.post_tool_use import handle_post_tool_use
        
        # Trigger post tool use where agent output quotes the chunk heading
        payload = json.dumps({
            'tool_name': 'Bash',
            'tool_input': {'command': 'git status'},
            'tool_response': 'We analyzed the Core Auth Logic here'
        })
        
        handle_post_tool_use(payload)
        
        quoted_file = Path(tmp_dir) / 'quoted.jsonl'
        assert quoted_file.exists()
        lines = quoted_file.read_text(encoding='utf-8').strip().split('\n')
        record = json.loads(lines[0])
        assert record['chunk_id'] == 'chunk123'

def test_pre_compact_context_prefetching(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LATTICE_VAULT_DIR", tmp_dir)
        
        # Setup mock chunks in vault
        vault = open_vault(tmp_dir)
        vault.db.execute("INSERT INTO chunks (id, heading, body, source, path, created_at, last_seen_at, last_validated_at) VALUES ('c100', 'h100', 'def some_code(): pass', 'code_index', 'p100', 'now', 'now', 'now')")
        vault.db.commit()
        vault.close()
        
        # Setup quoted.jsonl
        quoted_file = Path(tmp_dir) / 'quoted.jsonl'
        quoted_file.write_text(json.dumps({'chunk_id': 'c100', 'timestamp': 'now'}) + '\n', encoding='utf-8')
        
        from lattice.hooks.pre_compact import handle_pre_compact
        
        # Trigger pre-compaction
        out = handle_pre_compact(json.dumps([]))
        
        # Assert active-work.md is written
        active_file = Path(tmp_dir) / 'active-work.md'
        assert active_file.exists()
        assert "def some_code(): pass" in active_file.read_text(encoding='utf-8')
        
        # Assert context is prefetched and returned in output payload
        assert "def some_code(): pass" in out
        assert "h100" in out

