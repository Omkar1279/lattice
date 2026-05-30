import os
from pathlib import Path
from lattice.storage import open_vault
from lattice.util import truncate_to_budget

def handle_session_start(payload: str) -> str:
    vault_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    parts = []
    
    parts.append(
        '[lattice] Token-saving memory active. Previously-read files are indexed. '
        'When a Read is blocked, use lattice.recall_expand(chunk_id) to get the full file content — '
        'do NOT use Bash(cat ...) as a workaround. '
        'lattice.recall(query) searches all indexed content with a token budget. '
        'lattice.write(heading, body) persists decisions across sessions.'
    )
    
    try:
        vault = open_vault(vault_dir)
        cnt_row = vault.db.execute('SELECT COUNT(*) as cnt FROM chunks').fetchone()
        cnt = cnt_row['cnt'] if cnt_row else 0
        if cnt > 0:
            recent = vault.db.execute('SELECT heading FROM chunks ORDER BY last_seen_at DESC LIMIT 5').fetchall()
            headings = ', '.join([r['heading'] for r in recent])
            parts.append(f'{cnt} chunks indexed. Recent: {headings}.')
    except Exception:
        pass
    finally:
        try:
            vault.close()
        except Exception:
            pass

    summary_path = Path(vault_dir) / 'notes' / '_summary.md'
    if summary_path.exists():
        try:
            summary = summary_path.read_text(encoding='utf-8').strip()
            if summary:
                parts.append(summary)
        except Exception:
            pass
            
    out = truncate_to_budget('\n'.join(parts), 400)
    return out
