import typer
import os
import pathlib
import sys

app = typer.Typer(name='lattice', help='lattice CLI')
hook_app = typer.Typer(help='Claude Code hooks')
app.add_typer(hook_app, name='hook')

@app.command()
def init():
    '''Scaffold .lattice/ in the current repo'''
    v_dir = os.environ.get('LATTICE_VAULT_DIR', '.lattice')
    for d in ['notes', 'log']:
        (pathlib.Path(v_dir) / d).mkdir(parents=True, exist_ok=True)
    typer.echo('lattice: initialised .lattice/ — add to .gitignore if desired.')

@app.command()
def doctor(fix: bool = False):
    '''Verify hooks registered, grammars loaded, FTS populated, edges present'''
    typer.echo('Doctor command not fully implemented yet.')

@app.command()
def rebuild(notes_only: bool = False):
    '''Drop and rebuild the SQLite index from .lattice/notes/ Markdown and repo files'''
    typer.echo('Rebuild command not fully implemented yet.')

@app.command()
def audit(conflicts: bool = False, json: bool = False):
    '''List conflicts (chunks with overlapping headings/symbols)'''
    typer.echo('Audit command not fully implemented yet.')

@app.command(name='import')
def import_cmd(from_tool: str = typer.Option(..., '--from', help='basic-memory | memsearch | memory-bank | claude-md'), source: str = None):
    '''Import notes from another memory tool'''
    typer.echo('Import command not fully implemented yet.')

@app.command(name='mcp-server')
def mcp_server():
    '''Start the FastMCP daemon (stdio + HTTP)'''
    from lattice.daemon import run_daemon
    run_daemon()

# --- Hooks (thin clients) ---
# These commands are called by Claude Code's hook system.
# They delegate the heavy lifting to the background daemon via HTTP.

def _call_hook(name: str):
    from lattice.hooks._client import send_hook
    payload = sys.stdin.read() if not sys.stdin.isatty() else ''
    send_hook(name, payload)

@hook_app.command()
def session_start(): _call_hook('session-start')

@hook_app.command()
def pre_compact(): _call_hook('pre-compact')

@hook_app.command()
def post_tool_use(): _call_hook('post-tool-use')

@hook_app.command()
def pre_tool_use(): _call_hook('pre-tool-use')

@hook_app.command()
def stop(): _call_hook('stop')

if __name__ == '__main__':
    app()
