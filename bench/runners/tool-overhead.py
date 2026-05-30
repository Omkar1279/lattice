#!/usr/bin/env python3
"""
Python port of bench/runners/tool-overhead.ts
Tool-schema token overhead benchmark.
Spawns comparators, connects via stdio, queries tools/list, and counts input-tokens.
Writes to bench/results/tool-overhead.json.
"""

import os
import sys
import json
import tempfile
import shutil
import time
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from bench.lib.count_tokens import count_via_anthropic_api, spawn_and_list_tools

MODEL = os.environ.get("LATTICE_BENCH_MODEL", "claude-opus-4-7")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))

# Hand-authored Anthropic Memory Tool schema reconstructed from docs
ANTHROPIC_MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Store and retrieve information across conversations through a memory file "
        "directory at /memories. Persists between sessions. Commands: view "
        "(directory or file contents), create (new file), str_replace (edit text), "
        "insert (insert at line), delete (file or directory), rename (move)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "delete", "rename"],
                "description": "The memory operation to perform.",
            },
            "path": {"type": "string", "description": "Path under /memories. Required for view/create/str_replace/insert/delete."},
            "old_path": {"type": "string", "description": "Source path. Required for rename."},
            "new_path": {"type": "string", "description": "Destination path. Required for rename."},
            "file_text": {"type": "string", "description": "File contents. Required for create."},
            "old_str": {"type": "string", "description": "Text to replace. Required for str_replace."},
            "new_str": {"type": "string", "description": "Replacement text. Required for str_replace."},
            "insert_line": {"type": "integer", "description": "1-indexed line to insert at. Required for insert."},
            "insert_text": {"type": "string", "description": "Text to insert. Required for insert."},
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional [start, end] line range when viewing files.",
            },
        },
        "required": ["command"],
    },
}

COMPARATORS = [
    {
        "name": "lattice",
        "note": "subject under test; spawned from this python repo",
        "install": "(this repo) uv pip install -e .",
        "fallback": "skip",
        "spawn": {
            "command": sys.executable,
            "args": [os.path.join(REPO_ROOT, "lattice/daemon.py")],
            "env": {
                "LATTICE_VAULT_DIR": os.path.join(tempfile.gettempdir(), f"lattice-bench-vault-{int(time.time() * 1000)}")
            }
        }
    },
    {
        "name": "serena",
        "note": "Python-based MCP. Installed via uvx.",
        "install": "uvx --from git+https://github.com/oraios/serena serena start-mcp-server",
        "fallback": "skip",
        "spawn": {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/oraios/serena",
                "serena",
                "start-mcp-server"
            ]
        }
    },
    {
        "name": "mem0",
        "note": "requires OPENAI_API_KEY at startup.",
        "install": "npx -y mem0ai-mcp",
        "env_required": ["OPENAI_API_KEY"],
        "fallback": "skip",
        "spawn": {
            "command": "npx",
            "args": ["-y", "mem0ai-mcp"]
        }
    },
    {
        "name": "anthropic-memory-tool",
        "note": "1-tool floor hand-authored from public docs",
        "fallback": "source_reconstruction",
        "hand_authored": {
            "tools": [ANTHROPIC_MEMORY_SCHEMA],
            "doc_link": "https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/memory-tool"
        }
    },
    {
        "name": "cipher",
        "note": "skipped - listed for transparency, not measured",
        "fallback": "skip",
        "always_skipped_reason": "documented gap: no canonical package name, requires LLM API keys."
    }
]

# Baseline for prompt-tokens subtraction calibration
BASELINE_PROMPT = json.dumps({
    "note": (
        "baseline calibration prompt for the tool-overhead bench. fixed content; "
        "the model is instructed to reply 'ok' regardless. do not interpret."
    )
})

baseline_total = None

def get_tokens_for_schema(prompt: str) -> int:
    global baseline_total
    if baseline_total is None:
        baseline_total = count_via_anthropic_api(BASELINE_PROMPT)
    api_total = count_via_anthropic_api(prompt)
    return api_total - baseline_total

def measure_tools(server_name: str, api_tools: list, source: str, c: dict) -> dict:
    per_tool = []
    for tool in api_tools:
        # Standardize schema structure: name, description, input_schema
        norm_tool = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", tool.get("inputSchema", {}))
        }
        json_str = json.dumps(norm_tool)
        tokens = get_tokens_for_schema(json_str)
        per_tool.append({
            "name": tool["name"],
            "json_chars": len(json_str),
            "tokens": tokens
        })
        print(f"  {server_name}/{tool['name']}: {tokens} tokens", file=sys.stderr)
        
    total = sum(t["tokens"] for t in per_tool)
    return {
        "server": server_name,
        "status": "measured",
        "source": source,
        "install": c.get("install"),
        "note": c.get("note"),
        "tools_count": len(api_tools),
        "total_tokens": total,
        "per_tool": per_tool
    }

def main():
    import time
    started_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    results = []

    for c in COMPARATORS:
        name = c["name"]
        print(f"→ {name}", file=sys.stderr)

        if c.get("always_skipped_reason"):
            results.append({
                "server": name,
                "status": "skipped",
                "source": "n/a",
                "install": c.get("install"),
                "note": c.get("note"),
                "reason": c["always_skipped_reason"]
            })
            print(f"  skipped: {c['always_skipped_reason']}", file=sys.stderr)
            continue

        if c["fallback"] == "source_reconstruction" and c.get("hand_authored"):
            hand = c["hand_authored"]
            res = measure_tools(name, hand["tools"], "hand_authored_from_docs", c)
            res["doc_link"] = hand["doc_link"]
            results.append(res)
            continue

        # Spawn logic
        spawn_cfg = c.get("spawn")
        if not spawn_cfg:
            results.append({
                "server": name,
                "status": "skipped",
                "source": "n/a",
                "reason": "missing spawn config"
            })
            continue

        # Check required env vars
        missing_env = [var for var in c.get("env_required", []) if not os.environ.get(var)]
        if missing_env:
            results.append({
                "server": name,
                "status": "skipped",
                "source": "live_mcp",
                "install": c.get("install"),
                "note": c.get("note"),
                "reason": f"missing env: {', '.join(missing_env)}"
            })
            print(f"  skipped: missing env {missing_env}", file=sys.stderr)
            continue

        # Spawn and list
        vault_dir = spawn_cfg.get("env", {}).get("LATTICE_VAULT_DIR")
        try:
            tools = spawn_and_list_tools(
                spawn_cfg["command"],
                spawn_cfg["args"],
                env={**os.environ, **spawn_cfg.get("env", {})},
                timeout=15.0
            )
            res = measure_tools(name, tools, "live_mcp", c)
            results.append(res)
        except Exception as e:
            results.append({
                "server": name,
                "status": "skipped",
                "source": "live_mcp",
                "install": c.get("install"),
                "note": c.get("note"),
                "reason": f"spawn failed: {str(e)[:300]}"
            })
            print(f"  skipped: spawn failed: {e}", file=sys.stderr)
        finally:
            if vault_dir and os.path.exists(vault_dir):
                shutil.rmtree(vault_dir, ignore_errors=True)

    # Compute headline comparison
    lattice_res = next((r for r in results if r["server"] == "lattice" and r["status"] == "measured"), None)
    headline = []
    if lattice_res:
        for r in results:
            if r["status"] == "measured" and r["server"] != "lattice":
                headline.append({
                    "comparator": r["server"],
                    "competitor_total": r["total_tokens"],
                    "lattice_total": lattice_res["total_tokens"],
                    "tokens_saved_per_turn": r["total_tokens"] - lattice_res["total_tokens"],
                    "tokens_saved_per_50_turn_session": (r["total_tokens"] - lattice_res["total_tokens"]) * 50
                })

    output = {
        "meta": {
            "date": started_at,
            "model": MODEL,
            "python": sys.version,
            "platform": sys.platform,
            "tokenizer_source": "anthropic-api",
            "baseline_total_tokens": baseline_total
        },
        "results": results,
        "headline": headline
    }

    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../results'))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "tool-overhead.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"\n→ wrote {os.path.relpath(out_path, REPO_ROOT)}", file=sys.stderr)
    print(f"→ measured: {len([r for r in results if r['status'] == 'measured'])}, skipped: {len([r for r in results if r['status'] == 'skipped'])}", file=sys.stderr)

if __name__ == '__main__':
    main()
