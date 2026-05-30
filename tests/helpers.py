import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from lattice.storage.vault import open_vault, Vault

class TestVault:
    def __init__(self, vault: Vault, tmp_dir: str):
        self.vault = vault
        self.db = vault.db
        self.dir = tmp_dir
        self.tmp_dir = tmp_dir
        
    def close(self):
        self.vault.close()

    def insert_chunk(
        self,
        id: Optional[str] = None,
        heading: str = '',
        body: str = '',
        source: str = 'human_note',
        path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        created_at: Optional[str] = None,
        last_seen_at: Optional[str] = None,
        last_validated_at: Optional[str] = None,
        supersedes: Optional[str] = None,
        superseded_by: Optional[str] = None,
        pinned: int = 0
    ) -> Dict[str, Any]:
        chunk_id = id if id else uuid.uuid4().hex[:16]
        now = datetime.now().isoformat()
        chunk = {
            'id': chunk_id,
            'heading': heading,
            'body': body,
            'source': source,
            'path': path if path else f"/test/{chunk_id}.md",
            'tags': tags if tags is not None else [],
            'created_at': created_at if created_at else now,
            'last_seen_at': last_seen_at if last_seen_at else now,
            'last_validated_at': last_validated_at if last_validated_at else now,
            'supersedes': supersedes,
            'superseded_by': superseded_by,
            'pinned': pinned
        }

        self.db.execute(
            '''
            INSERT INTO chunks (
                id, heading, body, source, path, tags, created_at, 
                last_seen_at, last_validated_at, supersedes, superseded_by, pinned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                chunk['id'],
                chunk['heading'],
                chunk['body'],
                chunk['source'],
                chunk['path'],
                ','.join(chunk['tags']),
                chunk['created_at'],
                chunk['last_seen_at'],
                chunk['last_validated_at'],
                chunk['supersedes'],
                chunk['superseded_by'],
                chunk['pinned']
            )
        )
        self.db.commit()
        return chunk

def create_test_vault() -> TestVault:
    tmp_dir = tempfile.mkdtemp(prefix='lattice-test-')
    vault = open_vault(tmp_dir)
    return TestVault(vault, tmp_dir)
