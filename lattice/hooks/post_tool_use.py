import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any
from lattice.storage import open_vault
from lattice.util import count_tokens

TOKEN_THRESHOLD = 1000
EDIT_TOOLS = {'Edit', 'Write', 'MultiEdit'}
READ_TOOLS = {'Read', 'Grep', 'Bash', 'Cat'}

def track_quoted_chunks(vault_dir: str, payload: str):
    try:
        from pathlib import Path
        session_file = Path(vault_dir) / 'session_recalled.json'
        if not session_file.exists():
            return
            
        recalled = json.loads(session_file.read_text(encoding='utf-8'))
        if not recalled:
            return
            
        quoted_ids = set()
        for item in recalled:
            cid = item['id']
            heading = item['heading']
            if cid in payload or heading in payload:
                quoted_ids.add(cid)
                
        if quoted_ids:
            quoted_file = Path(vault_dir) / 'quoted.jsonl'
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            with open(quoted_file, 'a', encoding='utf-8') as f:
                for qid in quoted_ids:
                    f.write(json.dumps({'chunk_id': qid, 'timestamp': now}) + '\n')
    except Exception:
        pass

def handle_post_tool_use(payload: str) -> str:
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    track_quoted_chunks(vault_dir, payload)

    try:
        event = json.loads(payload)
    except Exception:
        return ''
        
    tool_name = event.get('tool_name', '')
    
    if tool_name in EDIT_TOOLS:
        handle_edit_tool(vault_dir, event)
        return ''
    elif tool_name in READ_TOOLS:
        return handle_read_tool(vault_dir, event)
        
    return ''

def handle_edit_tool(vault_dir: str, event: Dict[str, Any]):
    tool_input = event.get('tool_input', {})
    file_path = tool_input.get('file_path')
    if not file_path:
        return
        
    vault = open_vault(vault_dir)
    try:
        from lattice.indexer import index_file
        from lattice.indexer.graph import init_tree_sitter
        
        init_tree_sitter()
        pending_edges = []
        repo_root = os.getcwd()
        index_file(vault, file_path, repo_root, pending_edges)
        
        for pending in pending_edges:
            from lattice.indexer.graph import resolve_and_write_edges
            try:
                resolve_and_write_edges(vault, pending['chunk_id'], pending['file_path'], pending['raw_edges'], repo_root)
            except Exception:
                pass
        vault.db.commit()
    except Exception:
        pass
    finally:
        vault.close()

def handle_read_tool(vault_dir: str, event: Dict[str, Any]) -> str:
    result = event.get('tool_response') or event.get('tool_result')
    if result and isinstance(result, dict):
        if 'file' in result and isinstance(result['file'].get('content'), str):
            result = result['file']['content']
        elif isinstance(result.get('text'), str):
            result = result['text']
        elif isinstance(result.get('stdout'), str):
            result = result['stdout']
        elif isinstance(result.get('content'), str):
            result = result['content']
        elif isinstance(result.get('content'), list):
            result = '\n'.join([b.get('text', '') for b in result['content']])
    elif isinstance(result, list):
        result = '\n'.join([b.get('text', b.get('content', '')) for b in result])
        
    if not result or not isinstance(result, str):
        return ''
        
    tokens = count_tokens(result)
    if tokens < TOKEN_THRESHOLD:
        return ''
        
    chunk_id = hashlib.sha256(result.encode('utf-8')).hexdigest()[:16]
    tool_name = event.get('tool_name', 'unknown')
    tool_input = event.get('tool_input', {})
    target = tool_input.get('file_path') or tool_input.get('command') or tool_input.get('pattern') or 'result'
    heading = f'{tool_name}: {target}'
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    
    vault = open_vault(vault_dir)
    try:
        vault.db.execute('''
            INSERT INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at)
            VALUES (?, ?, ?, 'auto_capture', ?, 'tool_result', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at
        ''', (chunk_id, heading, result, tool_input.get('file_path', ''), now, now, now))
        vault.db.commit()
        
        # If Read of parseable file, trigger index
        if tool_name == 'Read' and tool_input.get('file_path'):
            from lattice.indexer import index_file
            from lattice.indexer.graph import init_tree_sitter
            init_tree_sitter()
            index_file(vault, tool_input['file_path'], os.getcwd(), [])
            vault.db.commit()
            
    finally:
        vault.close()
        
    return f'lattice: indexed {tokens} tokens as chunk_id={chunk_id}; this content is now recoverable via lattice.recall — consider expiring earlier read results from context.'
