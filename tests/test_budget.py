import pytest
from lattice.retrieval.cascade import CascadePipelineFactory
from helpers import create_test_vault

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def test_budget_tokens_invariant_returns_empty(vault):
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='nonexistent topic',
        budget_tokens=200,
        kind='all'
    )
    assert results == []

def test_budget_tokens_respects_budget(vault):
    for i in range(50):
        vault.insert_chunk(
            heading=f'Authentication handler {i}',
            body=f'This is a detailed authentication handler that processes JWT tokens and validates user sessions against the database. It includes rate limiting, CORS checks, and session expiration logic. Handler number {i} covers edge case {i}.',
            source='code_index'
        )
        
    budgets = [200, 500, 1000, 2500, 4000]
    for budget in budgets:
        pipeline = CascadePipelineFactory.create(vault.db)
        results = pipeline.retrieve(
            query='authentication handler',
            budget_tokens=budget,
            kind='all'
        )
        total_tokens = sum(r.tokens for r in results)
        assert total_tokens <= budget

def test_budget_tokens_returns_at_least_one(vault):
    vault.insert_chunk(
        heading='tiny note',
        body='hello world',
        source='human_note'
    )
    
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='tiny note',
        budget_tokens=200,
        kind='all'
    )
    
    assert len(results) >= 1
    total_tokens = sum(r.tokens for r in results)
    assert total_tokens <= 200

def test_budget_tokens_never_returns_single_preview_exceeding_budget(vault):
    vault.insert_chunk(
        heading='massive chunk',
        body='x' * 10000,
        source='code_index'
    )
    
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='massive chunk',
        budget_tokens=300,
        kind='all'
    )
    
    for r in results:
        assert r.tokens <= 300
