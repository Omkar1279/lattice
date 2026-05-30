import os
import httpx
from typing import List

def generate_queries(chunk_body: str, heading: str) -> List[str]:
    if os.environ.get("LATTICE_DOC2QUERY") != "on":
        return []
        
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []
        
    prompt = f'Given this code chunk titled "{heading}":\n\n{chunk_body[:1500]}\n\nGenerate exactly 5 short search queries a developer might use to find this chunk. One per line, no numbering, no bullets.'
    
    try:
        res = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10.0,
        )
        if res.status_code != 200:
            return []
            
        data = res.json()
        text = data.get("content", [{}])[0].get("text", "").strip()
        if not text:
            return []
            
        return [l.strip() for l in text.split("\n") if l.strip()][:5]
    except Exception:
        return []
