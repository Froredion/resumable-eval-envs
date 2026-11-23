"""Minimal deterministic stdio MCP server for tests.

State lives in a counter file under the directory given as argv[1], like an
MCP server persisting to app_data/.
"""

import json
import sys
from pathlib import Path

state = Path(sys.argv[1]) / "counter"


def call(name, args):
    if name == "incr":
        n = int(state.read_text()) if state.exists() else 0
        n += args.get("by", 1)
        state.write_text(str(n))
        return {"content": [{"type": "text", "text": str(n)}]}
    if name == "echo":
        return {"content": [{"type": "text", "text": args.get("text", "")}]}
    raise ValueError(name)


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notification
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        schema = {"type": "object", "properties": {}}
        result = {"tools": [
            {"name": "incr", "description": "Increment the counter and return its new value.", "inputSchema": schema},
            {"name": "echo", "description": "Return the given text.", "inputSchema": schema},
        ]}
    elif method == "tools/call":
        result = call(msg["params"]["name"], msg["params"].get("arguments", {}))
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
