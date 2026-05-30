import os
import re
import httpx
from typing import List

def extract_atomic_facts_regex(body: str) -> List[str]:
    lines = body.split("\n")
    facts: List[str] = []
    bullet_re = re.compile(r"^[-*•]\s+")
    sentence_split_re = re.compile(r"(?<=[.!?])\s+")
    
    for line in lines:
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
            
        if bullet_re.match(trimmed):
            content = bullet_re.sub("", trimmed).strip()
            if len(content) > 10:
                facts.append(content)
            continue
            
        sentences = sentence_split_re.split(trimmed)
        for s in sentences:
            clean = s.strip()
            if len(clean) > 10:
                facts.append(clean)
    return facts

def extract_atomic_facts_llm(body: str, api_key: str) -> List[str]:
    prompt = (
        "Extract atomic facts from this session log. One fact per line, no numbering, no bullets. "
        "Each fact must be self-contained and atomic (one assertion). Skip anything not actually "
        "decided or learned (questions, hedging, in-progress thoughts). Max 20 facts.\n\n"
        f"Session log:\n{body[:4000]}"
    )
    
    res = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=15.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}")
        
    data = res.json()
    text = data.get("content", [{}])[0].get("text", "")
    
    # Strip bullets/numbering
    strip_prefix_re = re.compile(r"^[-*•\d.)\s]+")
    facts = []
    for line in text.split("\n"):
        cleaned = strip_prefix_re.sub("", line.strip()).strip()
        if 10 < len(cleaned) < 500:
            facts.append(cleaned)
    return facts

def extract_atomic_facts(body: str) -> List[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return extract_atomic_facts_llm(body, api_key)
        except Exception:
            pass
    return extract_atomic_facts_regex(body)
