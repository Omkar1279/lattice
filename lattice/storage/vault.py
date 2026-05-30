import sqlite3
import sqlite_vec
import os
from pathlib import Path
from typing import Dict, Any, Optional

class Vault:
    '''Wrapper for the lattice storage (SQLite + Vector).'''
    def __init__(self, dir_path: str, db: sqlite3.Connection):
        self.dir = dir_path
        self.db = db
        
    def close(self):
        self.db.close()

def init_schema(db: sqlite3.Connection):
    '''Initializes the SQLite schema including FTS5 and Vector tables.'''
    db.executescript('''
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            heading TEXT NOT NULL,
            body TEXT NOT NULL,
            source TEXT NOT NULL,
            path TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_validated_at TEXT NOT NULL,
            supersedes TEXT,
            superseded_by TEXT,
            pinned INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            heading, body, tags, content='chunks', content_rowid='rowid'
        );
        CREATE TABLE IF NOT EXISTS symbols (
            symbol TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line INTEGER NOT NULL,
            kind TEXT NOT NULL,
            chunk_id TEXT,
            PRIMARY KEY (symbol, file_path, line)
        );
        CREATE TABLE IF NOT EXISTS edges (
            source_chunk_id TEXT NOT NULL,
            target_chunk_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            call_count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_chunk_id, target_chunk_id, kind)
        );
        CREATE INDEX IF NOT EXISTS edges_source_idx ON edges(source_chunk_id, kind);
        CREATE INDEX IF NOT EXISTS edges_target_idx ON edges(target_chunk_id, kind);
        CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
        CREATE INDEX IF NOT EXISTS chunks_last_seen_idx ON chunks(last_seen_at);

        CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, heading, body, tags) VALUES (new.rowid, new.heading, new.body, COALESCE(new.tags, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, heading, body, tags) VALUES('delete', old.rowid, old.heading, old.body, COALESCE(old.tags, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, heading, body, tags) VALUES('delete', old.rowid, old.heading, old.body, COALESCE(old.tags, ''));
            INSERT INTO chunks_fts(rowid, heading, body, tags) VALUES (new.rowid, new.heading, new.body, COALESCE(new.tags, ''));
        END;
    ''')

    # Vector table for semantic search (768 dimensions)
    has_vec = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_vec'").fetchone()
    if not has_vec:
        try:
            db.execute('CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[768])')
        except sqlite3.OperationalError:
            # vec0 module not loaded; semantic search won't work
            pass

def open_vault(vault_dir: str) -> Vault:
    '''Opens the vault and ensures directories and schema are ready.'''
    p = Path(vault_dir)
    for d in ['notes', 'log']: (p / d).mkdir(parents=True, exist_ok=True)
    
    db = sqlite3.connect(str(p / 'index.db'))
    db.row_factory = sqlite3.Row
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
    except AttributeError:
        # Some Python distributions disable extension loading; 
        # semantic search will be unavailable.
        pass
    
    init_schema(db)
    return Vault(vault_dir, db)
