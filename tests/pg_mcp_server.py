"""Stdio MCP server backed by the Postgres running in the same container.

``incr`` inserts a row and returns its id and created_at (DEFAULT now()),
so a rebuilt environment's database must replay ids and timestamps exactly.
With PG_HIDDEN_RANDOM=1 it also writes random() to a side table without
saying so: a side effect no tool result shows.
Uses psql over TCP (the image's init-time server is socket-only, so a TCP
connection means the real server is up).
"""

import json
import os
import subprocess
import sys
import time

PSQL = ["psql", "-h", "127.0.0.1", "-U", "postgres", "-qAt", "-v", "ON_ERROR_STOP=1", "-c"]


def sql(q):
    return subprocess.run([*PSQL, q], capture_output=True, text=True, check=True).stdout.strip()


deadline = time.monotonic() + 60
while subprocess.run(["pg_isready", "-h", "127.0.0.1", "-q"]).returncode != 0:
    if time.monotonic() > deadline:
        sys.exit("postgres did not come up")
    time.sleep(0.2)
sql("CREATE TABLE IF NOT EXISTS events (id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())")
HIDDEN = os.environ.get("PG_HIDDEN_RANDOM") == "1"
if HIDDEN:
    sql("CREATE TABLE IF NOT EXISTS side (id serial PRIMARY KEY, v float8 NOT NULL DEFAULT random())")

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "pg", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "incr", "description": "Insert an event row; return its id and created_at.",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        row = sql("SET TIME ZONE 'UTC'; INSERT INTO events DEFAULT VALUES RETURNING id || ' ' || created_at")
        if HIDDEN:
            sql("INSERT INTO side DEFAULT VALUES")
        result = {"content": [{"type": "text", "text": row}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
