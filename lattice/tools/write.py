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
    Delegates to the unified vault.write_note method.
    '''
    parsed = WriteArgs(**args)
    chunk = vault.write_note(
        heading=parsed.heading,
        body=parsed.body,
        tags=parsed.tags,
        source=parsed.source,
        supersedes=parsed.supersedes,
        pinned=parsed.pinned
    )
    return json.dumps({'chunk_id': chunk.id, 'path': chunk.path, 'status': 'success'}, indent=2)
