import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from lattice.storage import open_vault
from lattice.util import truncate_to_budget

RETENTION_DAYS = int(os.environ.get('LATTICE_RETENTION_DAYS', 365))

def handle_stop(payload: str) -> str:
    '''Final session hook: prunes old auto-captures, merges facts, and writes a session summary.'''
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    log_path = Path(vault_dir) / 'log' / 'hook.log'
    
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] stop fired\n")
    except Exception:
        pass

    vault = open_vault(vault_dir)
    db = vault.db
    
    try:
        if os.environ.get('LATTICE_AUTOSUMMARY') != 'off':
            write_summary(vault, vault_dir)
            
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat().replace('+00:00', 'Z')
        
        # Delete stale auto-captured chunks
        cursor = db.execute('DELETE FROM chunks WHERE source = "auto_capture" AND last_seen_at < ?', (cutoff,))
        if cursor.rowcount > 0:
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now(timezone.utc).isoformat()}] stop: pruned {cursor.rowcount} stale chunks (cutoff: {cutoff})\n")
            except Exception:
                pass
            
        db.commit()
    finally:
        vault.close()
        
    return ''

def write_summary(vault, vault_dir):
    '''Writes a markdown summary of the most recent notes and snapshots.'''
    recent_notes = vault.db.execute('''
        SELECT heading, body FROM chunks
        WHERE source = 'human_note' AND superseded_by IS NULL
        ORDER BY last_seen_at DESC LIMIT 5
    ''').fetchall()
    
    session_snapshot = vault.db.execute('''
        SELECT body FROM chunks
        WHERE source = 'auto_capture' AND tags LIKE '%session_snapshot%'
        ORDER BY last_seen_at DESC LIMIT 1
    ''').fetchone()
    
    lines = ['# lattice session summary\n']
    
    if recent_notes:
        lines.append('## Recent notes')
        for note in recent_notes:
            first_line = note['body'].split('\n')[0][:100]
            lines.append(f'- **{note["heading"]}**: {first_line}')
        lines.append('')
        
    if session_snapshot:
        lines.append('## Last session context')
        snapshot_lines = session_snapshot['body'].split('\n')[:5]
        lines.extend(snapshot_lines)
        lines.append('')
        
    if not recent_notes and not session_snapshot:
        lines.append('No notes yet. Use `lattice.recall(query)` to search or `lattice.write(...)` to persist facts.')
        
    summary = truncate_to_budget('\n'.join(lines), 300)
    summary_path = Path(vault_dir) / 'notes' / '_summary.md'
    summary_path.write_text(summary, encoding='utf-8')
