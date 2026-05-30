import sqlite3
import sqlite_vec
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from lattice.core.interfaces import Chunk

class Vault:
    '''Wrapper for the lattice storage (SQLite + Vector).'''
    def __init__(self, dir_path: str, db: sqlite3.Connection):
        self.dir = dir_path
        self.db = db
        
    def close(self):
        self.db.close()

    def write_edge(self, source: str, target: str, kind: str, confidence: float = 1.0):
        self.db.execute('''
            INSERT INTO edges (source_chunk_id, target_chunk_id, kind, confidence, call_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(source_chunk_id, target_chunk_id, kind) DO UPDATE SET
                call_count = call_count + 1,
                confidence = MAX(confidence, excluded.confidence)
        ''', (source, target, kind, confidence))

    def get_chunk(self, chunk_id: str) -> Optional[Chunk]:
        row = self.db.execute("SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL", (chunk_id,)).fetchone()
        return Chunk.from_dict(dict(row)) if row else None

    def get_chunk_raw(self, chunk_id: str) -> Optional[Chunk]:
        row = self.db.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return Chunk.from_dict(dict(row)) if row else None

    def write_note(
        self,
        heading: str,
        body: str,
        tags: List[str],
        source: str = 'human_note',
        supersedes: Optional[str] = None,
        pinned: bool = False
    ) -> Chunk:
        import uuid
        import yaml
        from datetime import datetime, timezone
        from lattice.retrieval.semantic import SemanticSearchStrategy
        
        chunk_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        file_path = f'{self.dir}/notes/{chunk_id}.md'
        
        tags_str = ','.join(tags)
        pinned_val = 1 if pinned else 0
        
        self.db.execute('''
            INSERT INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at, supersedes, pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                body = excluded.body,
                last_seen_at = excluded.last_seen_at,
                pinned = excluded.pinned
        ''', (
            chunk_id, heading, body, source, 
            file_path, tags_str, now, now, now, supersedes, pinned_val
        ))
        
        # Embed and store semantically
        semantic = SemanticSearchStrategy(self.db)
        semantic.embed_and_store(chunk_id, f'{heading}\n{body[:512]}')
        
        if supersedes:
            self.db.execute('UPDATE chunks SET superseded_by = ? WHERE id = ?', (chunk_id, supersedes))
            
        self.db.commit()
        
        # Save markdown source of truth
        try:
            md_dir = Path(self.dir) / 'notes'
            md_dir.mkdir(parents=True, exist_ok=True)
            
            frontmatter = {
                'id': chunk_id,
                'heading': heading,
                'tags': tags,
                'source': source,
                'created_at': now,
                'last_seen_at': now,
                'last_validated_at': now,
                'supersedes': supersedes
            }
            yaml_fm = yaml.dump(frontmatter, sort_keys=False).strip()
            (md_dir / f'{chunk_id}.md').write_text(f'---\n{yaml_fm}\n---\n# {heading}\n\n{body}', encoding='utf-8')
        except Exception:
            pass # Best effort for FS persistence
            
        return Chunk(
            id=chunk_id,
            heading=heading,
            body=body,
            source=source,
            path=file_path,
            tags=tags,
            created_at=now,
            last_seen_at=now,
            last_validated_at=now,
            supersedes=supersedes,
            pinned=pinned
        )

def migrate_expansion_queries(db: sqlite3.Connection):
    cursor = db.execute("PRAGMA table_info(chunks)")
    cols = [row[1] for row in cursor.fetchall()]
    if "expansion_queries" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN expansion_queries TEXT")

def migrate_pinned(db: sqlite3.Connection):
    cursor = db.execute("PRAGMA table_info(chunks)")
    cols = [row[1] for row in cursor.fetchall()]
    if "pinned" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

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
            expansion_queries TEXT,
            pinned INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            heading, body, tags, expansion_queries, content='chunks', content_rowid='rowid'
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
            INSERT INTO chunks_fts(rowid, heading, body, tags, expansion_queries) VALUES (new.rowid, new.heading, new.body, COALESCE(new.tags, ''), COALESCE(new.expansion_queries, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, heading, body, tags, expansion_queries) VALUES('delete', old.rowid, old.heading, old.body, COALESCE(old.tags, ''), COALESCE(old.expansion_queries, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, heading, body, tags, expansion_queries) VALUES('delete', old.rowid, old.heading, old.body, COALESCE(old.tags, ''), COALESCE(old.expansion_queries, ''));
            INSERT INTO chunks_fts(rowid, heading, body, tags, expansion_queries) VALUES (new.rowid, new.heading, new.body, COALESCE(new.tags, ''), COALESCE(new.expansion_queries, ''));
        END;
    ''')

    # Vector table for semantic search (384 dimensions)
    has_vec = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_vec'").fetchone()
    if has_vec:
        # Check if dimension matches 384. If it was 768, drop and recreate
        sql = has_vec[0]
        if "384" not in sql:
            db.execute("DROP TABLE chunks_vec")
            has_vec = None
            
    if not has_vec:
        try:
            db.execute('CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[384])')
        except sqlite3.OperationalError:
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
        pass
    
    init_schema(db)
    migrate_expansion_queries(db)
    migrate_pinned(db)
    
    return Vault(vault_dir, db)
