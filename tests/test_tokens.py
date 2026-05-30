import pytest
from lattice.util.tokens import CLAUDE4_TOKENIZER_CORRECTION, count_tokens, truncate_to_budget
import tiktoken

def test_claude4_tokenizer_correction_is_exported():
    assert CLAUDE4_TOKENIZER_CORRECTION == 1.78

def test_count_tokens_returns_0_for_empty_string():
    assert count_tokens('') == 0

def test_count_tokens_returns_ceil_for_typical_content():
    sample = 'The quick brown fox jumps over the lazy dog.'
    enc = tiktoken.get_encoding('cl100k_base')
    local = len(enc.encode(sample))
    
    import math
    expected = math.ceil(local * CLAUDE4_TOKENIZER_CORRECTION)
    assert count_tokens(sample) == expected

def test_count_tokens_strictly_exceeds_local_only_count_for_any_content():
    sample = 'function recall(query) { return retrieve(query); }'
    enc = tiktoken.get_encoding('cl100k_base')
    local = len(enc.encode(sample))
    assert count_tokens(sample) > local

def test_truncate_to_budget_preserves_strings_already_under_budget():
    s = 'short content'
    assert truncate_to_budget(s, 100) == s

def test_truncate_to_budget_returns_string_calibrated_count_le_budget():
    long_str = 'lorem ipsum dolor sit amet ' * 50
    result = truncate_to_budget(long_str, 100)
    assert count_tokens(result) <= 100
    assert result.endswith('…')

def test_truncate_to_budget_never_returns_more_than_budget_even_on_calibrated_counts():
    long_str = 'The quick brown fox jumps over the lazy dog. ' * 80
    for budget in [10, 50, 200, 500, 1000]:
        result = truncate_to_budget(long_str, budget)
        assert count_tokens(result) <= budget
