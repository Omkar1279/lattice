import pytest
from lattice.retrieval.cascade import CascadePipelineFactory
from lattice.tools.write import handle_write
from helpers import create_test_vault
import json

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def get_chunk(vault, chunk_id):
    row = vault.db.execute("SELECT * FROM chunks WHERE id = ? AND superseded_by IS NULL", (chunk_id,)).fetchone()
    return dict(row) if row else None

def get_chunk_raw(vault, chunk_id):
    row = vault.db.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return dict(row) if row else None

def test_supersedes_chunk_is_excluded_from_recall_results(vault):
    vault.insert_chunk(
        id='original-001',
        heading='Database connection string',
        body='postgres://localhost:5432/mydb',
        source='human_note'
    )
    
    vault.insert_chunk(
        id='replacement-001',
        heading='Database connection string',
        body='postgres://prod-server:5432/mydb?sslmode=require',
        source='human_note',
        supersedes='original-001'
    )
    
    vault.db.execute("UPDATE chunks SET superseded_by = ? WHERE id = ?", ('replacement-001', 'original-001'))
    vault.db.commit()
    
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='Database connection',
        budget_tokens=2500,
        kind='all'
    )
    
    ids = [r.chunk.id for r in results]
    assert 'original-001' not in ids
    assert 'replacement-001' in ids

def test_superseded_chunk_is_still_accessible_via_get_chunk_raw(vault):
    vault.insert_chunk(
        id='old-decision',
        heading='Architecture decision: monolith',
        body='We chose a monolithic architecture for simplicity.',
        source='human_note',
        superseded_by='new-decision'
    )
    
    vault.insert_chunk(
        id='new-decision',
        heading='Architecture decision: microservices',
        body='We moved to microservices for scalability.',
        source='human_note',
        supersedes='old-decision'
    )
    
    not_visible = get_chunk(vault.vault, 'old-decision')
    assert not_visible is None
    
    old_chunk = get_chunk_raw(vault.vault, 'old-decision')
    assert old_chunk is not None
    assert old_chunk['heading'] == 'Architecture decision: monolith'
    assert old_chunk['superseded_by'] == 'new-decision'

def test_write_note_correctly_marks_superseded_chunk(vault):
    vault.insert_chunk(
        id='target-chunk',
        heading='API rate limit',
        body='Rate limit is 100 req/min',
        source='human_note'
    )
    
    res = handle_write(vault.vault, {
        'heading': 'API rate limit',
        'body': 'Rate limit is 500 req/min (upgraded Q3 2025)',
        'tags': ['api', 'config'],
        'supersedes': 'target-chunk',
        'source': 'human_note'
    })
    new_note_id = json.loads(res)['chunk_id']
    
    old_chunk = get_chunk_raw(vault.vault, 'target-chunk')
    assert old_chunk['superseded_by'] == new_note_id
    
    new_chunk = get_chunk(vault.vault, new_note_id)
    assert new_chunk['supersedes'] == 'target-chunk'

def test_cascade_excludes_superseded_chunks_from_bm25_results(vault):
    vault.insert_chunk(
        id='v1-deploy',
        heading='deployment config v1',
        body='Deploy to us-east-1 with t3.medium instances',
        source='human_note',
        superseded_by='v2-deploy'
    )
    
    vault.insert_chunk(
        id='v2-deploy',
        heading='deployment config v2',
        body='Deploy to us-west-2 with t3.large instances, multi-AZ',
        source='human_note',
        supersedes='v1-deploy'
    )
    
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='deployment config',
        budget_tokens=2500,
        kind='all'
    )
    
    ids = [r.chunk.id for r in results]
    assert 'v1-deploy' not in ids
    assert 'v2-deploy' in ids

def test_chain_of_supersedes_only_latest_returned(vault):
    vault.insert_chunk(
        id='v1',
        heading='JWT secret rotation',
        body='Rotate every 90 days',
        source='human_note',
        superseded_by='v2'
    )
    
    vault.insert_chunk(
        id='v2',
        heading='JWT secret rotation',
        body='Rotate every 60 days',
        source='human_note',
        supersedes='v1',
        superseded_by='v3'
    )
    
    vault.insert_chunk(
        id='v3',
        heading='JWT secret rotation',
        body='Rotate every 30 days with automated key ceremony',
        source='human_note',
        supersedes='v2'
    )
    
    pipeline = CascadePipelineFactory.create(vault.db)
    results = pipeline.retrieve(
        query='JWT secret rotation',
        budget_tokens=2500,
        kind='all'
    )
    
    ids = [r.chunk.id for r in results]
    assert 'v1' not in ids
    assert 'v2' not in ids
    assert 'v3' in ids
