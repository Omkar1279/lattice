import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from helpers import create_test_vault
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
