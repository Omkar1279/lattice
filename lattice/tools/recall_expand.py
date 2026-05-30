import json
import os
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

from lattice.util import truncate_to_budget, count_tokens

class RecallExpandArgs(BaseModel):
    '''Arguments for expanding a specific chunk or exploring its relations.'''
    chunk_id: str = Field(...)
    mode: str = Field('body')
    budget_tokens: int = Field(1500, ge=200, le=8000)
    offset: int = Field(0, ge=0)

def expand_body(vault: Any, chunk: Dict[str, Any], budget: int) -> Dict[str, Any]:
    '''Returns the full chunk body and previews of adjacent chunks in the same file.'''
    body = truncate_to_budget(chunk.get('body', ''), budget)
    tokens_used = count_tokens(body)
    
    adjacent = []
    chunk_path = chunk.get('path')
    
    if chunk_path and tokens_used < budget:
        dir_path = os.path.dirname(chunk_path)
        if dir_path and len(dir_path) > 1:
            remaining = budget - tokens_used
            # Find sibling chunks in the same directory
            sql = '''
                SELECT id, heading, body FROM chunks
                WHERE path LIKE ? AND source = 'code_index' AND id != ?
                ORDER BY path LIMIT 4
            '''
            neighbours = [dict(r) for r in vault.db.execute(sql, (f'{dir_path}%', chunk['id'])).fetchall()]
            
            if neighbours:
                per_neighbour_cap = remaining // len(neighbours)
                for n in neighbours:
                    if remaining <= 0: break
                    # Take first 5 lines as a preview
                    preview_lines = n['body'].split('\n')[:5]
                    preview = truncate_to_budget('\n'.join(preview_lines), min(200, per_neighbour_cap))
                    toks = count_tokens(preview)
                    if toks > remaining: break
                    
                    adjacent.append({
                        'chunk_id': n['id'],
                        'heading': n['heading'],
                        'preview': preview
                    })
                    remaining -= toks
                    tokens_used += toks
                    
    return {
        'chunk_id': chunk['id'],
        'heading': chunk['heading'],
        'body': body,
        'source': chunk['source'],
        'last_seen_at': chunk['last_seen_at'],
        'budget': {'used': tokens_used, 'limit': budget},
        'adjacent': adjacent
    }

def expand_structural(vault: Any, source_id: str, mode: str, budget: int, offset: int) -> Dict[str, Any]:
    '''Explores the code graph (callers, imports, etc.) starting from a chunk.'''
    MODE_MAP = {
        'callers': {'kind': 'calls', 'dir': 'incoming'},
        'imports': {'kind': 'imports', 'dir': 'outgoing'},
        'dependents': {'kind': 'imports', 'dir': 'incoming'},
        'impl': {'kind': 'implements', 'dir': 'incoming'}
    }
    
    cfg = MODE_MAP.get(mode)
    if not cfg: return {'error': f'Invalid structural mode: {mode}'}
        
    source_chunk = vault.db.execute('SELECT path FROM chunks WHERE id = ?', (source_id,)).fetchone()
    source_dir = os.path.dirname(source_chunk['path'] if source_chunk else '')
    
    # Determine columns based on graph direction
    col = 'target_chunk_id' if cfg['dir'] == 'incoming' else 'source_chunk_id'
    join_col = 'source_chunk_id' if cfg['dir'] == 'incoming' else 'target_chunk_id'
    
    total_count = vault.db.execute(
        f'SELECT COUNT(*) as cnt FROM edges WHERE {col} = ? AND kind = ? AND confidence >= 0.5',
        (source_id, cfg['kind'])
    ).fetchone()['cnt']
    
    if total_count == 0:
        return {'chunk_id': source_id, 'mode': mode, 'results': [], 'total_count': 0}
        
    # Query related chunks, prioritizing those in the same directory
    sql = f'''
        SELECT c.*, e.confidence, e.call_count,
            CASE WHEN c.path LIKE ? THEN 1 ELSE 0 END as same_dir
        FROM edges e
        JOIN chunks c ON c.id = e.{join_col}
        WHERE e.{col} = ? AND e.kind = ? AND e.confidence >= 0.5
        ORDER BY same_dir DESC, e.call_count DESC, e.confidence DESC, c.last_seen_at DESC
        LIMIT 20 OFFSET ?
    '''
    related = [dict(r) for r in vault.db.execute(sql, (f'{source_dir}%', source_id, cfg['kind'], offset)).fetchall()]
    
    results = []
    tokens_used = 0
    for rel in related:
        preview = truncate_to_budget(rel['body'], min(600, budget - tokens_used))
        p_tokens = count_tokens(preview)
        if tokens_used + p_tokens > budget: break
            
        results.append({
            'chunk_id': rel['id'],
            'heading': rel['heading'],
            'body': preview,
            'path': rel['path']
        })
        tokens_used += p_tokens
        
    response = {
        'chunk_id': source_id,
        'mode': mode,
        'results': results,
        'total_count': total_count,
        'showing': {'count': len(results), 'offset': offset},
        'budget': {'used': tokens_used, 'limit': budget}
    }
    
    if total_count > offset + len(results):
        response['next_offset'] = offset + len(results)
        
    return response

def handle_recall_expand(vault: Any, args: Dict[str, Any]) -> str:
    '''Handler for the recall_expand tool.'''
    parsed = RecallExpandArgs(**args)
    row = vault.db.execute('SELECT * FROM chunks WHERE id = ?', (parsed.chunk_id,)).fetchone()
    if not row: raise ValueError(f'Chunk not found: {parsed.chunk_id}')
    
    chunk = dict(row)
    if parsed.mode == 'body':
        data = expand_body(vault, chunk, parsed.budget_tokens)
    else:
        data = expand_structural(vault, chunk['id'], parsed.mode, parsed.budget_tokens, parsed.offset)
        
    # Save recalled/expanded chunk IDs to .lattice/session_recalled.json
    try:
        from pathlib import Path
        session_file = Path(os.environ.get('LATTICE_VAULT_DIR', '.lattice')) / 'session_recalled.json'
        session_file.parent.mkdir(parents=True, exist_ok=True)
        recalled = []
        if session_file.exists():
            try:
                recalled = json.loads(session_file.read_text(encoding='utf-8'))
            except Exception:
                pass
        
        # Add primary chunk
        recalled.append({'id': chunk['id'], 'heading': chunk['heading']})
        
        # Add structural results if any
        if parsed.mode != 'body' and 'results' in data:
            for r in data['results']:
                recalled.append({'id': r['chunk_id'], 'heading': r['heading']})
                
        # Remove duplicates
        seen = set()
        unique = []
        for item in recalled:
            if item['id'] not in seen:
                seen.add(item['id'])
                unique.append(item)
        session_file.write_text(json.dumps(unique), encoding='utf-8')
    except Exception:
        pass

    return json.dumps(data, indent=2)
