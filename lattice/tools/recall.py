import json
import base64
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from lattice.retrieval import CascadePipelineFactory

class RecallArgs(BaseModel):
    '''Arguments for the recall tool.'''
    query: str = Field(..., description='Natural-language question OR a symbol identifier.')
    budget_tokens: int = Field(2500, ge=200, le=8000)
    kind: str = Field('auto')
    path_scope: Optional[str] = None
    since: Optional[str] = None
    continuation_token: Optional[str] = None

class ContinuationPayload(BaseModel):
    '''Data stored in the continuation token for pagination.'''
    offset: int
    query: str
    kind: str

def encode_continuation(payload: ContinuationPayload) -> str:
    '''Encode continuation data as base64url string.'''
    raw = payload.model_dump_json()
    return base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii').rstrip('=')

def decode_continuation(token: str) -> Optional[ContinuationPayload]:
    '''Decode continuation token back into a payload.'''
    try:
        padding = '=' * (4 - (len(token) % 4))
        raw = base64.urlsafe_b64decode(token + padding).decode('utf-8')
        return ContinuationPayload(**json.loads(raw))
    except Exception:
        return None

def handle_recall(vault: Any, args: Dict[str, Any]) -> str:
    '''
    Retrieves project context using a multi-stage retrieval cascade.
    1. Parses arguments and handles pagination via continuation tokens.
    2. Runs the full retrieval cascade (BM25, Semantic, Freshness, Symbol).
    3. Packs the top results into the requested token budget.
    4. Returns a JSON response with results and a new continuation token if more exist.
    '''
    parsed = RecallArgs(**args)
    
    offset = 0
    if parsed.continuation_token:
        payload = decode_continuation(parsed.continuation_token)
        if payload and payload.query == parsed.query and payload.kind == parsed.kind:
            offset = payload.offset

    # Run the cascade with a large enough internal budget to allow for pagination
    pipeline = CascadePipelineFactory.create(vault.db)
    all_results = pipeline.retrieve(
        query=parsed.query,
        kind=parsed.kind,
        path_scope=parsed.path_scope,
        budget_tokens=8000,
        since=parsed.since,
    )

    # Slice and pack results to fit the requested budget
    page_results = all_results[offset:]
    fitting_results = []
    used = 0

    for res in page_results:
        if used + res.tokens > parsed.budget_tokens:
            break
        fitting_results.append(res)
        used += res.tokens

    next_offset = offset + len(fitting_results)
    
    response = {
        'results': [
            {
                'id': r.chunk.id,
                'heading': r.chunk.heading,
                'preview': r.preview,
                'freshness': r.freshness,
                'tokens': r.tokens,
            }
            for r in fitting_results
        ],
        'budget': {
            'used': used,
            'limit': parsed.budget_tokens,
            'remaining_chunks': len(all_results) - next_offset
        },
        'continuation_token': None
    }

    if next_offset < len(all_results):
        response['continuation_token'] = encode_continuation(ContinuationPayload(
            offset=next_offset,
            query=parsed.query,
            kind=parsed.kind
        ))

    # Save recalled chunk IDs to .lattice/session_recalled.json
    try:
        from pathlib import Path
        import os
        session_file = Path(os.environ.get('LATTICE_VAULT_DIR', '.lattice')) / 'session_recalled.json'
        session_file.parent.mkdir(parents=True, exist_ok=True)
        recalled = []
        if session_file.exists():
            try:
                recalled = json.loads(session_file.read_text(encoding='utf-8'))
            except Exception:
                pass
        for r in response['results']:
            recalled.append({'id': r['id'], 'heading': r['heading']})
        # Remove duplicates
        seen = set()
        unique = []
        for item in recalled:
            if item['id'] not in seen:
                seen.add(item['id'])
                unique.append(item)
        session_file.write_text(json.dumps(unique), encoding='utf-8')
    except Exception:
        pass

    return json.dumps(response, indent=2)
