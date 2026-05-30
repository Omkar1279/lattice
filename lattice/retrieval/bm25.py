"""BM25 full-text search strategy."""

import re
from typing import Any, List, Optional

from lattice.core.interfaces import Chunk, SearchStrategy


class QuerySanitizer:
    """Responsible for sanitizing queries for FTS5 (SRP)."""

    def sanitize(self, query: str) -> str:
        """Sanitize query for SQLite FTS5 MATCH clause.

        Joins tokens with OR to improve recall for natural language queries.
        """
        tokens = [t for t in re.split(r'\W+', query) if t]
        if not tokens:
            return ''
        safe_tokens = [f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in tokens]
        return ' OR '.join(safe_tokens)


class BM25SearchStrategy(SearchStrategy):
    """Full-text search using SQLite FTS5 (Strategy Pattern).

    Follows SRP: only does BM25 keyword search.
    Follows DIP: depends on SearchStrategy abstraction.
    """

    def __init__(self, db: Any, sanitizer: Optional[QuerySanitizer] = None):
        self._db = db
        self._sanitizer = sanitizer or QuerySanitizer()

    def search(
        self,
        query: str,
        limit: int,
        path_scope: Optional[str] = None,
        source_filter: Optional[List[str]] = None,
    ) -> List[Chunk]:
        fts_query = self._sanitizer.sanitize(query)
        if not fts_query:
            return []

        conditions = ['chunks_fts MATCH ?']
        params: List[Any] = [fts_query]

        if path_scope:
            conditions.append('c.path LIKE ?')
            params.append(f'{path_scope}%')

        if source_filter:
            placeholders = ','.join('?' for _ in source_filter)
            conditions.append(f'c.source IN ({placeholders})')
            params.extend(source_filter)

        conditions.append('c.superseded_by IS NULL')
        params.append(limit)

        sql = f'''
            SELECT c.*
            FROM chunks_fts fts
            JOIN chunks c ON c.rowid = fts.rowid
            WHERE {' AND '.join(conditions)}
            ORDER BY rank
            LIMIT ?
        '''

        rows = [dict(row) for row in self._db.execute(sql, params).fetchall()]
        return [Chunk.from_dict(r) for r in rows]


