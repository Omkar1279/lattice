"""File indexing: AST chunking + storage."""

import os
import hashlib
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from lattice.indexer.graph import get_parser, extract_edges

TARGET_CHUNK_CHARS = 2048


@dataclass
class ChunkData:
    """Pure data from chunking — no DB dependency."""
    id: str
    heading: str
    body: str
    start_line: int
    end_line: int


def make_chunk_id(file_path: str, start_line: int) -> str:
    hash_input = f'{file_path}:{start_line}'.encode('utf-8')
    return hashlib.sha256(hash_input).hexdigest()[:16]


def chunk_file(file_path: str, source: str) -> list[ChunkData]:
    """Parse and chunk a file into ChunkData objects. Pure, no side effects."""
    ext = os.path.splitext(file_path)[1]
    file_name = os.path.basename(file_path)
    parser = get_parser(ext)

    if parser:
        tree = parser.parse(source.encode('utf8'))
        raw_chunks = _ast_chunk(tree, source)
    else:
        raw_chunks = [{'start_line': 0, 'end_line': max(0, len(source.splitlines()) - 1), 'body': source}]

    results = []
    for chunk in raw_chunks:
        chunk_id = make_chunk_id(file_path, chunk['start_line'])
        if len(raw_chunks) == 1:
            heading = file_name
        else:
            heading = f"{file_name}:{chunk['start_line'] + 1}-{chunk['end_line'] + 1}"
        results.append(ChunkData(
            id=chunk_id,
            heading=heading,
            body=chunk['body'],
            start_line=chunk['start_line'],
            end_line=chunk['end_line'],
        ))
    return results


def _ast_chunk(tree: Any, source: str) -> list[dict[str, Any]]:
    """Greedily merge AST siblings under TARGET_CHUNK_CHARS budget."""
    root = tree.root_node
    if not root.children:
        return [{'start_line': 0, 'end_line': max(0, len(source.splitlines()) - 1), 'body': source}]

    chunks = []
    current_nodes = []
    current_chars = 0

    for child in root.children:
        node_text = child.text.decode('utf8') if isinstance(child.text, bytes) else child.text
        node_chars = len(node_text)

        if current_chars + node_chars > TARGET_CHUNK_CHARS and current_nodes:
            chunks.append(_flush_nodes(current_nodes))
            current_nodes = []
            current_chars = 0

        if node_chars > TARGET_CHUNK_CHARS and not current_nodes:
            chunks.append({
                'start_line': child.start_point[0],
                'end_line': child.end_point[0],
                'body': node_text,
            })
            continue

        current_nodes.append(child)
        current_chars += node_chars

    if current_nodes:
        chunks.append(_flush_nodes(current_nodes))

    return chunks


def _flush_nodes(nodes: list) -> dict[str, Any]:
    return {
        'start_line': nodes[0].start_point[0],
        'end_line': nodes[-1].end_point[0],
        'body': '\n'.join(
            n.text.decode('utf8') if isinstance(n.text, bytes) else n.text
            for n in nodes
        ),
    }


def index_file(vault: Any, file_path: str, repo_root: str, pending_edges: list[dict[str, Any]] | None = None) -> None:
    """Read, chunk, and store a file into the vault."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception:
        return

    now = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    chunks = chunk_file(file_path, source)
    if not chunks:
        return

    from lattice.retrieval.semantic import SemanticSearchStrategy
    from lattice.retrieval.contextual import contextualize_chunk
    from lattice.retrieval.doc2query import generate_queries
    from lattice.retrieval.colbert import embed_and_store_colbert
    
    semantic = SemanticSearchStrategy(vault.db)
    file_context = source[:300]

    for chunk in chunks:
        # Contextual retrieval: prepend context summary if enabled
        contextualized_body = contextualize_chunk(file_path, chunk.body, file_context)
        
        # Doc2Query: generate expansion queries if enabled
        queries = generate_queries(chunk.body, chunk.heading)
        expansion_queries_str = "\n".join(queries) if queries else None

        vault.db.execute('''
            INSERT INTO chunks (id, heading, body, source, path, tags, created_at, last_seen_at, last_validated_at, expansion_queries)
            VALUES (?, ?, ?, 'code_index', ?, '', ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                body = excluded.body,
                heading = excluded.heading,
                last_seen_at = excluded.last_seen_at,
                last_validated_at = excluded.last_validated_at,
                expansion_queries = excluded.expansion_queries
        ''', (chunk.id, chunk.heading, contextualized_body, file_path, now, now, now, expansion_queries_str))
        
        # Semantic embedding uses contextualized body
        semantic.embed_and_store(chunk.id, f'{chunk.heading}\n{contextualized_body[:512]}')
        
        # Colbert embedding if enabled
        embed_and_store_colbert(vault.db, chunk.id, f'{chunk.heading}\n{contextualized_body[:512]}')

    # Store export symbols and collect edges
    raw_edges = extract_edges(file_path, source)
    primary_chunk_id = chunks[0].id

    for edge in raw_edges:
        if edge['kind'] == 'exports':
            vault.db.execute('''
                INSERT OR REPLACE INTO symbols (symbol, file_path, line, kind, chunk_id)
                VALUES (?, ?, ?, 'export', ?)
            ''', (edge['target_symbol'], file_path, edge['line'], primary_chunk_id))

    if pending_edges is not None:
        pending_edges.append({
            'chunk_id': primary_chunk_id,
            'file_path': file_path,
            'raw_edges': raw_edges,
        })


def should_ignore(file_path: str, repo_root: str) -> bool:
    import fnmatch
    rel_path = os.path.relpath(file_path, repo_root)
    parts = rel_path.split(os.sep)
    
    ignore_dirs = {
        'node_modules', '.git', '.terraform', '.next', '.nuxt',
        'dist', 'build', '.lattice', '.venv', '.pytest_cache', '__pycache__'
    }
    for p in parts[:-1]:
        if p in ignore_dirs:
            return True
            
    filename = parts[-1]
    ignore_patterns = [
        '.env', '.env.*', '*.key', '*.pem', '*.p12', '*.pfx',
        'id_rsa*', '*secret*', '*credential*'
    ]
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(filename, pattern):
            return True
            
    return False


def reindex_repo(vault: Any, repo_root: str) -> None:
    """Recursively index repo, excluding ignored paths and removing stale chunks."""
    from lattice.indexer.graph import init_tree_sitter
    init_tree_sitter()
    
    repo_root = os.path.abspath(repo_root)
    parseable_exts = {".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go"}
    
    files_to_index = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in {
            'node_modules', '.git', '.terraform', '.next', '.nuxt',
            'dist', 'build', '.lattice', '.venv', '.pytest_cache', '__pycache__'
        }]
        for file in files:
            full_path = os.path.join(root, file)
            if should_ignore(full_path, repo_root):
                continue
            ext = os.path.splitext(file)[1]
            if ext in parseable_exts:
                files_to_index.append(full_path)
                
    # Prune chunks for files that no longer exist
    existing_files = set(files_to_index)
    cursor = vault.db.execute("SELECT id, path FROM chunks WHERE source = 'code_index'")
    existing_chunks = [dict(r) for r in cursor.fetchall()]
    for chunk in existing_chunks:
        if chunk['path'] not in existing_files:
            vault.db.execute("DELETE FROM chunks WHERE id = ?", (chunk['id'],))
            vault.db.execute("DELETE FROM edges WHERE source_chunk_id = ? OR target_chunk_id = ?", (chunk['id'], chunk['id']))
            vault.db.execute("DELETE FROM symbols WHERE chunk_id = ?", (chunk['id'],))
            
    # Index all found files
    pending_edges = []
    for file_path in files_to_index:
        index_file(vault, file_path, repo_root, pending_edges)
        
    # Resolve and write edges
    from lattice.indexer.graph import resolve_and_write_edges
    for pending in pending_edges:
        try:
            resolve_and_write_edges(vault, pending['chunk_id'], pending['file_path'], pending['raw_edges'], repo_root)
        except Exception:
            pass
        
    vault.db.commit()

