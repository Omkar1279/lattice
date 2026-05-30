import json
import uuid
import os
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

class WriteArgs(BaseModel):
    '''Arguments for persisting a fact or note.'''
    heading: str = Field(..., min_length=1, max_length=120)
    body: str = Field(..., min_length=1, max_length=4000)
    tags: List[str] = Field(default_factory=list)
    supersedes: Optional[str] = None
    source: str = Field('human_note')
    pinned: bool = Field(False)

def handle_write(vault: Any, args: Dict[str, Any]) -> str:
    '''
    Persists a note to both the SQLite database and a markdown file.
    1. Generates a unique ID and timestamp.
    2. Inserts metadata and content into the 'chunks' table.
    3. Updates 'supersedes' relationships to maintain consistency.
    4. Triggers embedding generation for semantic search.
    5. Saves a markdown file with YAML frontmatter as the source of truth.
    '''
    parsed = WriteArgs(**args)
    chunk_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    file_path = f'.lattice/notes/{chunk_id}.md'
    
    # Store in database
    vault.db.execute('''
        INSERT INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at, supersedes, pinned)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        chunk_id, parsed.heading, parsed.body, parsed.source, 
        file_path, ','.join(parsed.tags), now, now, now, parsed.supersedes, 1 if parsed.pinned else 0
    ))
    
    if parsed.supersedes:
        vault.db.execute('UPDATE chunks SET superseded_by = ? WHERE id = ?', (chunk_id, parsed.supersedes))
        
    vault.db.commit()
    
    # Generate embeddings
    from lattice.retrieval import SemanticSearchStrategy
    semantic = SemanticSearchStrategy(vault.db)
    semantic.embed_and_store(chunk_id, f'{parsed.heading}\n{parsed.body[:512]}')
    
    # Save markdown source of truth
    try:
        md_dir = Path(vault.dir) / 'notes'
        md_dir.mkdir(parents=True, exist_ok=True)
        
        frontmatter = {
            'id': chunk_id,
            'source': parsed.source,
            'created_at': now,
            'tags': parsed.tags
        }
        if parsed.supersedes:
            frontmatter['supersedes'] = parsed.supersedes
            
        yaml_fm = yaml.dump(frontmatter, sort_keys=False).strip()
        (md_dir / f'{chunk_id}.md').write_text(f'---\n{yaml_fm}\n---\n# {parsed.heading}\n\n{parsed.body}', encoding='utf-8')
    except Exception:
        pass # Best effort for FS persistence
    
    return json.dumps({'chunk_id': chunk_id, 'path': file_path, 'status': 'success'}, indent=2)
