import os
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from lattice.storage import open_vault
from lattice.util import count_tokens

MAX_SNAPSHOT_TOKENS = 3000

def handle_pre_compact(payload: str) -> str:
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    prefetched_context = ''
    
    # Process quoted.jsonl to prefetch context
    try:
        from collections import Counter
        quoted_file = Path(vault_dir) / 'quoted.jsonl'
        if quoted_file.exists():
            quoted_ids = []
            with open(quoted_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        if 'chunk_id' in record:
                            quoted_ids.append(record['chunk_id'])
                    except Exception:
                        pass
                        
            if quoted_ids:
                counts = Counter(quoted_ids)
                top_k = [item[0] for item in counts.most_common(3)]
                
                vault = open_vault(vault_dir)
                try:
                    prefetched = []
                    for cid in top_k:
                        row = vault.db.execute('SELECT * FROM chunks WHERE id = ?', (cid,)).fetchone()
                        if row:
                            prefetched.append(dict(row))
                            
                    if prefetched:
                        active_path = Path(vault_dir) / 'active-work.md'
                        active_lines = ['# Active Work Context (Prefetched for Compaction)\n']
                        for p in prefetched:
                            active_lines.append(f"### Chunk: {p['heading']} (ID: {p['id']})")
                            active_lines.append(f"Path: {p['path']}\n")
                            active_lines.append(p['body'])
                            active_lines.append("\n---\n")
                        active_path.write_text('\n'.join(active_lines), encoding='utf-8')
                        
                        ctx_lines = ['[lattice] Active work context prefetched from previous conversation history:']
                        for p in prefetched:
                            ctx_lines.append(f"Heading: {p['heading']} (ID: {p['id']})")
                            ctx_lines.append(p['body'])
                            ctx_lines.append('')
                        prefetched_context = '\n'.join(ctx_lines)
                finally:
                    vault.close()
    except Exception:
        pass

    try:
        transcript = json.loads(payload)
    except Exception:
        return prefetched_context
        
    messages = []
    if isinstance(transcript, list):
        messages = transcript
    elif isinstance(transcript, dict):
        messages = transcript.get('messages', transcript.get('conversation', []))
        
    if not messages:
        return prefetched_context
        
    assistant_messages = [m for m in messages if m.get('role') == 'assistant'][-10:]
    if not assistant_messages:
        return ''
        
    fact_patterns = [
        re.compile(r'^[-*]\s+', re.MULTILINE),
        re.compile(r'\b(decided|chose|selected|using|switched to|migrated)\b', re.IGNORECASE),
        re.compile(r'\b(note|important|caveat|constraint|requirement)\s*:', re.IGNORECASE),
        re.compile(r'\b(created|implemented|added|fixed|removed|refactored)\b', re.IGNORECASE),
        re.compile(r'\b(pattern|architecture|convention|approach)\b', re.IGNORECASE),
    ]
    
    extracted_lines = []
    total_tokens = 0
    
    for msg in assistant_messages:
        content = msg.get('content', '')
        if isinstance(content, list):
            content = '\n'.join([c.get('text', '') for c in content if isinstance(c, dict)])
        
        if not isinstance(content, str):
            continue
            
        lines = content.split('\n')
        for line in lines:
            trimmed = line.strip()
            if len(trimmed) < 10 or len(trimmed) > 500:
                continue
                
            if not any(p.search(trimmed) for p in fact_patterns):
                continue
                
            line_tokens = count_tokens(trimmed)
            if total_tokens + line_tokens > MAX_SNAPSHOT_TOKENS:
                break
                
            extracted_lines.append(trimmed)
            total_tokens += line_tokens
            
        if total_tokens >= MAX_SNAPSHOT_TOKENS:
            break
            
    if not extracted_lines:
        return ''
        
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    vault = open_vault(vault_dir)
    
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    body = '\n'.join(extracted_lines)
    chunk_id = uuid.uuid4().hex[:16]
    heading = f'Session snapshot ({now[:10]})'
    
    try:
        vault.db.execute('''
            INSERT INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at)
            VALUES (?, ?, ?, 'auto_capture', '', 'session_snapshot', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                body = excluded.body,
                last_seen_at = excluded.last_seen_at
        ''', (chunk_id, heading, body, now, now, now))
        vault.db.commit()
        
        snapshot_path = Path(vault_dir) / 'notes' / '_session.md'
        md_content = f'---\nid: {chunk_id}\nsource: auto_capture\ncreated_at: {now}\ntags: [session_snapshot]\n---\n# {heading}\n\n{body}\n'
        snapshot_path.write_text(md_content, encoding='utf-8')
        
        summary_path = Path(vault_dir) / 'notes' / '_summary.md'
        summary_lines = ['# lattice session summary\n', '## Session context (pre-compaction)'] + extracted_lines[:10] + ['']
        summary_path.write_text('\n'.join(summary_lines), encoding='utf-8')
    finally:
        vault.close()
        
    return prefetched_context
