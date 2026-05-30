import math
import tiktoken
import os

# Calibrated correction factor for Claude-4 vs Claude-1 BPE.
CLAUDE4_TOKENIZER_CORRECTION = 1.78

def count_tokens(s: str) -> int:
    '''Count tokens in a string using tiktoken with a correction factor.'''
    if not s:
        return 0
    try:
        # Use tiktoken cl100k_base as approximation, but multiply by the correction factor
        enc = tiktoken.get_encoding('cl100k_base')
        local_count = len(enc.encode(s, disallowed_special=()))
        return math.ceil(local_count * CLAUDE4_TOKENIZER_CORRECTION)
    except Exception:
        # Emergency fallback
        return math.ceil((len(s) / 4) * CLAUDE4_TOKENIZER_CORRECTION)

def truncate_to_budget(s: str, budget: int) -> str:
    '''Truncate a string to fit within a token budget.'''
    if count_tokens(s) <= budget:
        return s
    
    lo = 0
    hi = len(s)
    
    while lo < hi:
        mid = (lo + hi + 1) >> 1
        if count_tokens(s[:mid] + '…') <= budget:
            lo = mid
        else:
            hi = mid - 1
            
    return s[:lo] + '…'
