import pytest
from helpers import create_test_vault
from lattice.retrieval.symbol import GraphExpandedSymbolResolver


@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()


def insert_symbol(vault, symbol, file_path, line, chunk_id):
    vault.db.execute(
        'INSERT INTO symbols (symbol, file_path, line, kind, chunk_id) VALUES (?, ?, ?, ?, ?)',
        (symbol, file_path, line, 'export', chunk_id),
    )
    vault.db.commit()


def insert_edge(vault, source_id, target_id, kind='calls', confidence=0.9):
    vault.db.execute(
        'INSERT INTO edges (source_chunk_id, target_chunk_id, kind, confidence) VALUES (?, ?, ?, ?)',
        (source_id, target_id, kind, confidence),
    )
    vault.db.commit()


class TestGraphExpandedSymbolResolver:
    def test_resolves_exact_symbol_to_chunk(self, vault):
        vault.insert_chunk(id='chunk-a', heading='utils.ts', body='export function parseJSON() {}', source='code_index')
        insert_symbol(vault, 'parseJSON', '/project/utils.ts', 1, 'chunk-a')

        resolver = GraphExpandedSymbolResolver(vault.db)
        results = resolver.resolve('parseJSON')

        assert len(results) == 1
        assert results[0].id == 'chunk-a'

    def test_returns_empty_for_unknown_symbol(self, vault):
        resolver = GraphExpandedSymbolResolver(vault.db)
        results = resolver.resolve('NonExistentSymbol')
        assert results == []

    def test_path_scope_filters(self, vault):
        vault.insert_chunk(id='c1', heading='a.ts', body='export class Foo {}', source='code_index', path='/proj/src/a.ts')
        vault.insert_chunk(id='c2', heading='b.ts', body='export class Foo {}', source='code_index', path='/proj/lib/b.ts')
        insert_symbol(vault, 'Foo', '/proj/src/a.ts', 1, 'c1')
        insert_symbol(vault, 'Foo', '/proj/lib/b.ts', 1, 'c2')

        resolver = GraphExpandedSymbolResolver(vault.db)

        src_results = resolver.resolve('Foo', path_scope='/proj/src')
        assert len(src_results) == 1
        assert src_results[0].id == 'c1'

        all_results = resolver.resolve('Foo')
        assert len(all_results) == 2

    def test_excludes_superseded_chunks(self, vault):
        vault.insert_chunk(id='old', heading='old.ts', body='old impl', source='code_index', superseded_by='new')
        vault.insert_chunk(id='new', heading='new.ts', body='new impl', source='code_index')
        insert_symbol(vault, 'MyFunc', '/proj/old.ts', 1, 'old')
        insert_symbol(vault, 'MyFunc', '/proj/new.ts', 1, 'new')

        resolver = GraphExpandedSymbolResolver(vault.db)
        results = resolver.resolve('MyFunc')

        ids = [r.id for r in results]
        assert 'old' not in ids
        assert 'new' in ids

    def test_expands_callers_one_hop(self, vault):
        vault.insert_chunk(id='target', heading='service.ts', body='export function process() {}', source='code_index')
        vault.insert_chunk(id='caller1', heading='handler.ts', body='import { process } from "./service"; process();', source='code_index')
        vault.insert_chunk(id='caller2', heading='worker.ts', body='import { process } from "./service"; process();', source='code_index')
        insert_symbol(vault, 'process', '/proj/service.ts', 1, 'target')
        insert_edge(vault, 'caller1', 'target')
        insert_edge(vault, 'caller2', 'target')

        resolver = GraphExpandedSymbolResolver(vault.db)
        results = resolver.resolve('process')

        ids = [r.id for r in results]
        assert ids[0] == 'target'  # primary first
        assert 'caller1' in ids
        assert 'caller2' in ids

    def test_respects_max_callers_limit(self, vault):
        vault.insert_chunk(id='target', heading='core.ts', body='export function x() {}', source='code_index')
        insert_symbol(vault, 'x', '/proj/core.ts', 1, 'target')

        for i in range(10):
            vault.insert_chunk(id=f'caller-{i}', heading=f'c{i}.ts', body=f'x()', source='code_index')
            insert_edge(vault, f'caller-{i}', 'target')

        resolver = GraphExpandedSymbolResolver(vault.db, max_callers=3)
        results = resolver.resolve('x')

        # 1 target + at most 3 callers
        assert len(results) <= 4

    def test_low_confidence_edges_excluded(self, vault):
        vault.insert_chunk(id='target', heading='t.ts', body='export function y() {}', source='code_index')
        vault.insert_chunk(id='weak', heading='w.ts', body='y()', source='code_index')
        insert_symbol(vault, 'y', '/proj/t.ts', 1, 'target')
        insert_edge(vault, 'weak', 'target', confidence=0.3)  # below 0.5 threshold

        resolver = GraphExpandedSymbolResolver(vault.db)
        results = resolver.resolve('y')

        ids = [r.id for r in results]
        assert 'weak' not in ids
