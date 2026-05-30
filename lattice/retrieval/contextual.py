import os
import json
import hashlib
import atexit
import signal
import sys
from pathlib import Path
import httpx
from typing import Dict

_cache: Dict[str, str] = None
_cache_pending_writes = 0
_first_error_logged = False

def default_cache_file() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "lattice" / "contextual.json"

def cache_file_path() -> Path:
    env_path = os.environ.get("LATTICE_CONTEXTUAL_CACHE_FILE")
    return Path(env_path) if env_path else default_cache_file()

def load_cache() -> Dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    
    if os.environ.get("LATTICE_CONTEXTUAL_CACHE") == "off":
        _cache = {}
        return _cache
        
    p = cache_file_path()
    try:
        if p.exists():
            _cache = json.loads(p.read_text(encoding="utf-8"))
        else:
            _cache = {}
    except Exception:
        _cache = {}
    return _cache

def flush_cache() -> None:
    global _cache_pending_writes
    if _cache is None or _cache_pending_writes == 0:
        return
    if os.environ.get("LATTICE_CONTEXTUAL_CACHE") == "off":
        return
    try:
        p = cache_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_cache), encoding="utf-8")
        _cache_pending_writes = 0
    except Exception:
        pass

def install_flush_hooks():
    atexit.register(flush_cache)
    
    def signal_handler(signum, frame):
        flush_cache()
        sys.exit(128 + signum)
        
    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except ValueError:
        # Can fail if not in main thread
        pass

# Install hooks on import
install_flush_hooks()

def cache_key(file_path: str, chunk_body: str, file_context: str) -> str:
    h = hashlib.sha256()
    h.update(b"v1\0")
    h.update(file_path.encode("utf-8"))
    h.update(b"\0")
    h.update(chunk_body.encode("utf-8"))
    h.update(b"\0")
    h.update(file_context[:300].encode("utf-8"))
    return h.hexdigest()

def in_path_scope(file_path: str) -> bool:
    scope = os.environ.get("LATTICE_CONTEXTUAL_PATH_SCOPE")
    if not scope:
        return True
    return file_path.startswith(scope)

def log_first_error(msg: str) -> None:
    global _first_error_logged
    if os.environ.get("LATTICE_CONTEXTUAL_VERBOSE") == "on":
        sys.stderr.write(f"[contextual] {msg}\n")
        return
    if _first_error_logged:
        return
    _first_error_logged = True
    sys.stderr.write(f"[contextual] {msg} — further errors suppressed (set LATTICE_CONTEXTUAL_VERBOSE=on to see all).\n")

def model_name() -> str:
    return os.environ.get("LATTICE_CONTEXTUAL_MODEL", "claude-haiku-4-5")

def contextualize_chunk(file_path: str, chunk_body: str, file_context: str) -> str:
    global _cache_pending_writes
    if os.environ.get("LATTICE_CONTEXTUAL_CHUNKS") != "on":
        return chunk_body
    if not in_path_scope(file_path):
        return chunk_body
        
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return chunk_body
        
    c = load_cache()
    key = cache_key(file_path, chunk_body, file_context)
    cached_prefix = c.get(key)
    if cached_prefix is not None:
        return f"{cached_prefix}\n\n{chunk_body}" if cached_prefix else chunk_body
        
    prompt = f'Here is a chunk from the file "{file_path}". The file\'s overall purpose: {file_context[:300]}\n\nChunk:\n{chunk_body[:1500]}\n\nWrite a 1-2 sentence context prefix (50-100 tokens) describing what this chunk does and where it fits in the file. Output ONLY the prefix, nothing else.'
    
    try:
        model = model_name()
        res = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10.0,
        )
        if res.status_code != 200:
            log_first_error(f"HTTP {res.status_code} from Anthropic (model={model})")
            return chunk_body
            
        data = res.json()
        prefix = data.get("content", [{}])[0].get("text", "").strip()
        if not prefix:
            c[key] = ""
            _cache_pending_writes += 1
            return chunk_body
            
        c[key] = prefix
        _cache_pending_writes += 1
        
        if _cache_pending_writes >= 25:
            flush_cache()
            
        return f"{prefix}\n\n{chunk_body}"
    except Exception as e:
        log_first_error(f"network error: {str(e)}")
        return chunk_body

def _reset_contextual_cache_for_tests() -> None:
    global _cache, _cache_pending_writes, _first_error_logged
    _cache = None
    _cache_pending_writes = 0
    _first_error_logged = False
