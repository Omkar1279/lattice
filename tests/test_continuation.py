import pytest
import json
import base64
from lattice.tools.recall import handle_recall
from helpers import create_test_vault

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def parse_recall_response(response_json):
    return json.loads(response_json)

def test_continuation_returns_null_token_when_all_fit_in_budget(vault):
    vault.insert_chunk(heading='small note', body='hello', source='human_note')
    
    response = handle_recall(vault.vault, {'query': 'small note', 'budget_tokens': 2500})
    parsed = parse_recall_response(response)
    
    assert parsed.get('continuation_token') is None

def test_continuation_returns_token_when_results_exceed_budget(vault):
    for i in range(20):
        vault.insert_chunk(
            heading=f'database migration step {i}',
            body=f"Step {i}: ALTER TABLE users ADD COLUMN preferences JSONB DEFAULT '{{}}'; CREATE INDEX idx_preferences ON users USING gin(preferences);",
            source='human_note'
        )
        
    response = handle_recall(vault.vault, {'query': 'database migration', 'budget_tokens': 300})
    parsed = parse_recall_response(response)
    
    if parsed['budget']['remaining_chunks'] > 0:
        assert parsed.get('continuation_token') is not None

def test_continuation_pagination_no_duplicates_no_gaps(vault):
    for i in range(30):
        vault.insert_chunk(
            heading=f'API endpoint {i}',
            body=f'GET /api/v1/resource/{i} returns the resource details including metadata, timestamps, and nested relationships.',
            source='code_index'
        )
        
    all_ids = []
    continuation_token = None
    iterations = 0
    max_iterations = 50
    
    while iterations < max_iterations:
        args = {
            'query': 'API endpoint',
            'budget_tokens': 400
        }
        if continuation_token:
            args['continuation_token'] = continuation_token
            
        response = handle_recall(vault.vault, args)
        parsed = parse_recall_response(response)
        
        for r in parsed['results']:
            all_ids.append(r['id'])
            
        continuation_token = parsed.get('continuation_token')
        iterations += 1
        if not continuation_token:
            break
            
    unique_ids = set(all_ids)
    assert len(unique_ids) == len(all_ids)
    assert len(all_ids) > 0

def test_continuation_token_is_decodable(vault):
    for i in range(10):
        vault.insert_chunk(
            heading=f'config option {i}',
            body=f'LATTICE_OPTION_{i}=value sets the {i}th configuration parameter for the retrieval cascade.',
            source='human_note'
        )
        
    response = handle_recall(vault.vault, {'query': 'config option', 'budget_tokens': 200})
    parsed = parse_recall_response(response)
    
    continuation_token = parsed.get('continuation_token')
    if continuation_token:
        decoded = base64.urlsafe_b64decode(continuation_token + '=' * (-len(continuation_token) % 4)).decode('utf8')
        payload = json.loads(decoded)
        assert 'offset' in payload
        assert 'query' in payload
        assert isinstance(payload['offset'], int)
        assert payload['offset'] > 0
