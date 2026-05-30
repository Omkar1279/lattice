"""Symbol resolution with 1-hop graph expansion."""

from typing import Any, Dict, List, Optional

from lattice.core.interfaces import Chunk, SymbolResolver


class GraphExpandedSymbolResolver(SymbolResolver):
    """Resolves symbols with 1-hop graph expansion (callers).

    Follows SRP: single responsibility of resolving symbol names to chunks.
    Follows OCP: expansion logic can be overridden in subclasses.
    """

    def __init__(self, db: Any, max_symbols: int = 5, max_callers: int = 3):
        self._db = db
        self._max_symbols = max_symbols
        self._max_callers = max_callers

    def resolve(self, query: str, path_scope: Optional[str] = None) -> List[Chunk]:
        symbol_rows = self._find_symbols(query, path_scope)
        if not symbol_rows:
            return []

        results = self._resolve_chunks(symbol_rows)
        if not results:
            return []

        self._expand_callers(results)
        return results

    def _find_symbols(self, query: str, path_scope: Optional[str]) -> List[Dict[str, Any]]:
        if path_scope:
            sql = (
                'SELECT * FROM symbols WHERE symbol = ? '
                'AND file_path LIKE ? AND chunk_id IS NOT NULL LIMIT ?'
            )
            params = [query, f'{path_scope}%', self._max_symbols]
        else:
            sql = 'SELECT * FROM symbols WHERE symbol = ? AND chunk_id IS NOT NULL LIMIT ?'
            params = [query, self._max_symbols]

        return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _resolve_chunks(self, symbol_rows: List[Dict[str, Any]]) -> List[Chunk]:
        seen_ids: set = set()
        results: List[Chunk] = []

        for row in symbol_rows:
            chunk_id = row['chunk_id']
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            chunk_row = self._db.execute(
                'SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL', [chunk_id]
            ).fetchone()
            if chunk_row:
                results.append(Chunk.from_dict(dict(chunk_row)))

        return results

    def _expand_callers(self, results: List[Chunk]) -> None:
        """1-hop expansion: include up to N callers of the primary result."""
        if not results:
            return

        primary_id = results[0].id
        seen_ids = {c.id for c in results}

        caller_edges = self._db.execute(
            '''
            SELECT source_chunk_id FROM edges
            WHERE target_chunk_id = ? AND kind = 'calls' AND confidence >= 0.5
            ORDER BY confidence DESC LIMIT ?
            ''',
            [primary_id, self._max_callers],
        ).fetchall()

        for edge in caller_edges:
            source_id = edge['source_chunk_id']
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            caller_chunk = self._db.execute(
                'SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL', [source_id]
            ).fetchone()
            if caller_chunk:
                results.append(Chunk.from_dict(dict(caller_chunk)))


