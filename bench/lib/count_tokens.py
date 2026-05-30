import os
import sys
import json
import subprocess
import time

import urllib.request
import urllib.error

def count_via_anthropic_api(prompt: str, model: str = None) -> int:
    """
    Count tokens via Anthropic's count_tokens API endpoint (which is free/0 money).
    """
    # Load API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        key_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.anthropic-key"))
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as f:
                api_key = f.read().strip()
                
    if not api_key:
        raise ValueError(
            "Anthropic API key not found. Please set ANTHROPIC_API_KEY environment variable "
            "or create a `.anthropic-key` file in the repository root."
        )
        
    if model is None:
        model = os.environ.get("LATTICE_BENCH_MODEL", "claude-opus-4-7")
        
    url = "https://api.anthropic.com/v1/messages/count_tokens"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "token-counting-2024-11-01",
        "content-type": "application/json"
    }
    
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data.get("input_tokens", 0)
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode("utf-8")
        except Exception:
            err_msg = str(e)
        raise RuntimeError(f"Anthropic API token counting failed (HTTP {e.code}): {err_msg}")
    except Exception as e:
        raise RuntimeError(f"Failed to call Anthropic Tokenizer API: {e}")

def spawn_and_list_tools(command: str, args: list, env: dict = None, cwd: str = None, timeout: float = 15.0) -> list:
    """
    Spawn an MCP server over stdio, run standard JSON-RPC handshake to list tools, then close.
    Duplicates the TS spawnAndListTools function.
    """
    if env is None:
        env = os.environ.copy()
        
    cmd = [command] + args
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd
    )
    
    try:
        # 1. Send initialize request
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "lattice-bench",
                    "version": "0.1.0"
                }
            }
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        
        # Read response line-by-line
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stream during initialize")
            
        init_resp = json.loads(line)
        if "error" in init_resp:
            raise RuntimeError(f"MCP server initialize failed: {init_resp['error']}")
            
        # 2. Send initialized notification
        init_notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        proc.stdin.write(json.dumps(init_notif) + "\n")
        proc.stdin.flush()
        
        # 3. Send tools/list request
        list_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }
        proc.stdin.write(json.dumps(list_req) + "\n")
        proc.stdin.flush()
        
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stream during tools/list")
            
        list_resp = json.loads(line)
        if "error" in list_resp:
            raise RuntimeError(f"MCP server tools/list failed: {list_resp['error']}")
            
        tools = list_resp.get("result", {}).get("tools", [])
        return tools
        
    finally:
        # Try to terminate process cleanly
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            proc.kill()
