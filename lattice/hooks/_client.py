import os
import sys
import json
import socket
import urllib.request
import subprocess
from pathlib import Path

def get_port():
    # Try to read the port from state file or calculate the default
    state_file = Path.home() / '.lattice' / 'state.json'
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            return state.get('port')
        except Exception:
            pass
    return int(os.environ.get('LATTICE_PORT', 37700 + (os.getuid() % 100)))

def spawn_daemon():
    # Spawn the daemon in a new session so it outlives the hook
    env = os.environ.copy()
    # Let the daemon know it was spawned by a hook
    env['LATTICE_DAEMON_AUTO_SPAWNED'] = '1'
    subprocess.Popen(
        [sys.executable, '-m', 'lattice.cli', 'mcp-server'],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env
    )

def send_hook(hook_name: str, payload: str):
    # Short-circuit if agent_id is present (prevents subagents from race-spawning daemons)
    # The agent_id might be passed via env or in the payload, but standard subagent invocation 
    # typically provides AGENT_ID env var in Claude Code.
    if 'CLAUDE_AGENT_ID' in os.environ or 'AGENT_ID' in os.environ:
        sys.exit(0)

    port = get_port()
    url = f'http://127.0.0.1:{port}/hook/{hook_name}'
    
    data = payload.encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req, timeout=1.0) as response:
            result = response.read().decode('utf-8')
            if result:
                sys.stdout.write(result)
            sys.exit(0)
    except (urllib.error.URLError, socket.error):
        # Daemon is not running or not responding. Spawn it.
        spawn_daemon()
        # We don't wait for it to boot. The hook's work is either lost or we could queue it,
        # but for simplicity we just exit and let the next hook succeed.
        sys.exit(0)
