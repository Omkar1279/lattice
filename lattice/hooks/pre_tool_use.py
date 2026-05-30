import os
import json
import re
from typing import Dict, Any
from pathlib import Path
from lattice.storage import open_vault
from lattice.retrieval.freshness import ExponentialFreshnessScorer
from lattice.core.interfaces import Chunk
import time

BLOCK_MIN_CHARS = int(os.environ.get('LATTICE_BLOCK_MIN_CHARS', 40000))

_scorer = ExponentialFreshnessScorer()

def handle_pre_tool_use(payload: str) -> str:
    '''Handles the PreToolUse hook to block redundant large-file reads.'''
    try:
        event = json.loads(payload)
    except Exception:
        return ''
        
    tool_name = event.get('tool_name', '')
    tool_input = event.get('tool_input', {})
    
    target = None
    if tool_name == 'Read':
        target = tool_input.get('file_path')
    elif tool_name == 'Glob':
        target = tool_input.get('pattern')
    elif tool_name in ('Grep', 'Bash'):
        target = tool_input.get('pattern') or tool_input.get('regex') or tool_input.get('command')
        
    if not target: return ''
        
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    vault = open_vault(vault_dir)
    try:
        if tool_name == 'Read':
            return handle_read(vault, target, tool_input)
        elif tool_name == 'Bash':
            return handle_bash(vault, event)
        elif tool_name in ('Grep', 'Glob'):
            return handle_grep_glob(vault, target, tool_name)
    finally:
        vault.close()
        
    return ''

def handle_read(vault, target: str, tool_input: Dict[str, Any]) -> str:
    '''Decision logic for blocking Read calls.'''
    absolute = os.path.abspath(target)
    basename = os.path.basename(target)
    
    row = vault.db.execute('''
        SELECT id, heading, source, path, length(body) as body_len, last_seen_at, pinned 
        FROM chunks WHERE path = ? OR path LIKE ? ORDER BY last_seen_at DESC LIMIT 1
    ''', (absolute, f'%/{basename}')).fetchone()
    
    if not row: return ''
    if tool_input.get('offset') is not None and tool_input.get('limit') is not None and tool_input['limit'] < 50:
        return ''
    if row['body_len'] < BLOCK_MIN_CHARS:
        return ''
        
    now_ms = time.time() * 1000
    freshness = _scorer.score(Chunk.from_dict(dict(row)), now_ms)
    
    if freshness > 0.8:
        reason = f'"{basename}" freshness={freshness:.2f} (recently validated). Use lattice.recall_expand("{row["id"]}") instead.'
        return json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': reason
            }
        })
        
    return ''

def handle_bash(vault, event: Dict[str, Any]) -> str:
    '''Blocks cat commands for files already indexed and fresh.'''
    cmd = event.get('tool_input', {}).get('command', '')
    match = re.match(r'^cat\s+([^\s|;&]+)\s*$', cmd)
    if not match: return ''
        
    file_path = match.group(1)
    absolute = os.path.abspath(file_path)
    basename = os.path.basename(file_path)
    
    row = vault.db.execute('''
        SELECT id, source, path, length(body) as body_len, last_seen_at, pinned 
        FROM chunks WHERE path = ? OR path LIKE ? ORDER BY last_seen_at DESC LIMIT 1
    ''', (absolute, f'%/{basename}')).fetchone()
    
    if not row or row['body_len'] < BLOCK_MIN_CHARS: return ''
        
    now_ms = time.time() * 1000
    freshness = _scorer.score(Chunk.from_dict(dict(row)), now_ms)
    if freshness < 0.3: return ''
        
    reason = f'"cat {basename}" blocked — file is indexed (chunk_id={row["id"]}, freshness={freshness:.2f}). Use lattice.recall_expand("{row["id"]}") instead.'
    return json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason
        }
    })

def handle_grep_glob(vault, target: str, tool_name: str) -> str:
    '''Suggests using recall if multiple matches are found in the index.'''
    clean_pattern = re.sub(r'[.*+?^${}()|[\]\\]', ' ', target).strip()
    if len(clean_pattern) < 3: return ''
        
    fts_query = ' OR '.join([f'"{t.replace('"', '""')}"' for t in clean_pattern.split() if len(t) > 2])
    if not fts_query: return ''
        
    try:
        hit = vault.db.execute('SELECT COUNT(*) as cnt FROM chunks_fts WHERE chunks_fts MATCH ?', (fts_query,)).fetchone()
        if hit and hit['cnt'] > 0:
            strength = 'STRONGLY recommend' if hit['cnt'] > 5 else 'Consider'
            return f'lattice: {hit["cnt"]} indexed chunk(s) match "{clean_pattern}". {strength} lattice.recall("{clean_pattern}") instead of {tool_name}.\n'
    except Exception:
        pass
        
    return ''
