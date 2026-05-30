import pytest
from helpers import create_test_vault
from lattice.retrieval.bm25 import BM25SearchStrategy

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()

def test_bm25_multi_word_query_returns_docs_containing_all_terms(vault):
    vault.insert_chunk(
        id='all-three',
        heading='all-three.py',
        body='alpha beta gamma all in the same document'
    )
    vault.insert_chunk(
        id='unrelated',
        heading='u.py',
        body='completely unrelated content here'
    )
    
    results = BM25SearchStrategy(vault.db).search('alpha beta gamma', 10)
    ids = [r.id for r in results]
    assert 'all-three' in ids

def test_bm25_multi_word_query_surfaces_docs_containing_any_term(vault):
    vault.insert_chunk(
        id='all-terms',
        heading='all.py',
        body='alpha beta gamma alpha beta gamma in same document'
    )
    vault.insert_chunk(
        id='alpha-only',
        heading='a.py',
        body='alpha appears here only and no other query terms'
    )
    vault.insert_chunk(
        id='beta-only',
        heading='b.py',
        body='beta appears here only and no other query terms'
    )
    vault.insert_chunk(
        id='unrelated',
        heading='u.py',
        body='nothing relevant in this document at all'
    )
    
    results = BM25SearchStrategy(vault.db).search('alpha beta gamma', 10)
    ids = [r.id for r in results]
    
    assert 'all-terms' in ids
    assert 'alpha-only' in ids
    assert 'beta-only' in ids
    
    all_rank = ids.index('all-terms')
    assert all_rank < ids.index('alpha-only')
    assert all_rank < ids.index('beta-only')

def test_bm25_regression_case_from_sanity_bench(vault):
    vault.insert_chunk(
        id='source',
        heading='exceptions.py',
        path='/repo/fastapi/exceptions.py',
        body='''class HTTPException(StarletteHTTPException):
    """Raise this from a route to emit an HTTP error response.
    Inherits from Starlette's HTTPException and adds support for
    structured detail payloads. HTTPException is the canonical way
    to signal request errors. Defined here, used everywhere."""

    def __init__(self, status_code: int, detail = None, headers = None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(status_code=status_code, detail=detail, headers=headers)
'''
    )
    
    for i in range(1, 4):
        vault.insert_chunk(
            id=f'tut-{i}',
            heading=f'tutorial{i}.py',
            path=f'/repo/docs_src/tutorial{i}.py',
            body=f'from fastapi import HTTPException\n\nasync def handler(): raise HTTPException(status_code=404)\n'
        )
        
    noise_topics = ['config', 'schema', 'database', 'client', 'router']
    for i, topic in enumerate(noise_topics):
        vault.insert_chunk(
            id=f'noise-{i}',
            heading=f'{topic}.py',
            path=f'/repo/lib/{topic}.py',
            body=f'# Where is the {topic} defined? It is in another module.\n# This file is where the {topic} configuration is defined.\n# We use defaults defined elsewhere when {topic} is not set.\n'
        )
        
    vault.insert_chunk(
        id='stopwords',
        heading='main.py',
        path='/repo/main.py',
        body="# Where is the configuration defined? Is it here? It is not.\n# This file deliberately uses 'where' and 'is' and 'defined'\n# but says nothing about HTTP errors.\n"
    )
    
    results = BM25SearchStrategy(vault.db).search('where is HTTPException defined?', 10)
    ids = [r.id for r in results]
    
    assert 'source' in ids
    
    source_rank = ids.index('source')
    stopwords_rank = ids.index('stopwords') if 'stopwords' in ids else float('inf')
    
    assert source_rank < stopwords_rank
    assert source_rank < 5
