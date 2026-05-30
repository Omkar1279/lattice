import os
import json
import threading
import logging
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from mcp.server.fastmcp import FastMCP
from typing import Optional, List

# FastMCP server instance for the 'lattice' toolset
mcp = FastMCP('lattice')

def prewarm_models():
    '''Warms up AI models in a background thread to avoid latency on first tool use.'''
    logging.info('Pre-warming models...')
    try:
        from lattice.retrieval import create_embedder, create_reranker
        create_embedder()
        create_reranker()
        from lattice.indexer import init_tree_sitter
        init_tree_sitter()
    except Exception as e:
        logging.error(f'Error pre-warming models: {e}')

class HookHandler(BaseHTTPRequestHandler):
    '''
    Handles HTTP POST requests from the Claude daemon hooks.
    Each hook (pre-tool-use, etc.) is routed to its respective handler.
    '''
    def do_POST(self):
        parts = self.path.strip('/').split('/')
        if len(parts) != 2 or parts[0] != 'hook':
            self.send_response(404)
            self.end_headers()
            return
            
        hook_name = parts[1]
        length = int(self.headers.get('Content-Length', 0))
        payload = self.rfile.read(length).decode('utf-8')
        
        try:
            # Route to hook handlers; dynamic imports keep the daemon startup light
            if hook_name == 'session-start':
                from lattice.hooks.session_start import handle_session_start as h
            elif hook_name == 'pre-compact':
                from lattice.hooks.pre_compact import handle_pre_compact as h
            elif hook_name == 'post-tool-use':
                from lattice.hooks.post_tool_use import handle_post_tool_use as h
            elif hook_name == 'pre-tool-use':
                from lattice.hooks.pre_tool_use import handle_pre_tool_use as h
            elif hook_name == 'stop':
                from lattice.hooks.stop import handle_stop as h
            else:
                self.send_response(404)
                self.end_headers()
                return
                
            response_text = h(payload)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            if response_text:
                self.wfile.write(response_text.encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

    def log_message(self, format, *args):
        # Quiet logs to avoid interference with MCP stdio
        pass

@mcp.tool()
def recall(query: str, budget_tokens: int = 2500, kind: str = 'auto', path_scope: Optional[str] = None, since: Optional[str] = None, continuation_token: Optional[str] = None) -> str:
    '''Retrieve relevant project context (code symbols, notes, prior decisions) for a query.'''
    from lattice.storage import open_vault
    from lattice.tools import handle_recall
    
    vault = open_vault(os.environ.get('LATTICE_VAULT_DIR', '.lattice'))
    try:
        return handle_recall(vault, locals())
    finally:
        vault.close()

@mcp.tool()
def recall_expand(chunk_id: str, mode: str = 'body', budget_tokens: int = 1500, offset: int = 0) -> str:
    '''Expand a chunk by ID or explore its relations (callers, imports, etc.).'''
    from lattice.storage import open_vault
    from lattice.tools import handle_recall_expand
    
    # Telemetry logging
    try:
        telemetry_dir = Path(os.environ.get('LATTICE_VAULT_DIR', '.lattice')) / 'log'
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        with open(telemetry_dir / 'telemetry.log', 'a', encoding='utf-8') as f:
            f.write(f"recall_expand:{mode}\n")
    except Exception:
        pass
        
    vault = open_vault(os.environ.get('LATTICE_VAULT_DIR', '.lattice'))
    try:
        return handle_recall_expand(vault, locals())
    finally:
        vault.close()

@mcp.tool()
def write(heading: str, body: str, tags: Optional[List[str]] = None, supersedes: Optional[str] = None, source: str = 'human_note', pinned: bool = False) -> str:
    '''Persist a fact, decision, or note. One focused statement per call.'''
    from lattice.storage import open_vault
    from lattice.tools import handle_write
    
    vault = open_vault(os.environ.get('LATTICE_VAULT_DIR', '.lattice'))
    try:
        return handle_write(vault, locals())
    finally:
        vault.close()

def run_daemon():
    '''Starts the background models, HTTP hook server, and the main FastMCP loop.'''
    threading.Thread(target=prewarm_models, daemon=True).start()
    
    port = int(os.environ.get('LATTICE_PORT', 37700 + (os.getuid() % 100)))
    state_dir = Path.home() / '.lattice'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'state.json').write_text(json.dumps({'port': port}))
    
    threading.Thread(target=lambda: HTTPServer(('127.0.0.1', port), HookHandler).serve_forever(), daemon=True).start()
    mcp.run()

if __name__ == '__main__':
    run_daemon()
