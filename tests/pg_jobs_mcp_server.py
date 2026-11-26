"""Stdio MCP server for a shop whose orders are shipped by a background worker.

``order`` queues an order (status pending) and returns at once; a worker
thread ships each pending order a second later (monotonic time: the pinned
clock does not move within a step). ``cancel`` cancels the last order unless
it has shipped. So ``cancel``'s answer depends on whether the worker got
there first: in the original run the model's thinking time lets it; a
replay is fast enough to beat it, unless the rebuild waits for the queue to
drain. Uses psql over TCP, like pg_mcp_server.py.
"""

import json
import subprocess
import sys
import threading
import time

PSQL = ["psql", "-h", "127.0.0.1", "-U", "postgres", "-qAt", "-v", "ON_ERROR_STOP=1", "-c"]


def sql(q):
    return subprocess.run([*PSQL, q], capture_output=True, text=True, check=True).stdout.strip()


deadline = time.monotonic() + 60
while subprocess.run(["pg_isready", "-h", "127.0.0.1", "-q"]).returncode != 0:
    if time.monotonic() > deadline:
        sys.exit("postgres did not come up")
    time.sleep(0.2)
sql("CREATE TABLE IF NOT EXISTS orders (id serial PRIMARY KEY, status text NOT NULL)")


def worker():
    while True:
        pending = sql("SELECT id FROM orders WHERE status = 'pending' ORDER BY id LIMIT 1")
        if pending:
            time.sleep(1)
            sql(f"UPDATE orders SET status = 'shipped' WHERE id = {pending} AND status = 'pending'")
        else:
            time.sleep(0.1)


threading.Thread(target=worker, daemon=True).start()

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "shop", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}}
                            for n in ("order", "cancel")]}
    elif method == "tools/call" and msg["params"]["name"] == "order":
        n = sql("INSERT INTO orders (status) VALUES ('pending') RETURNING id")
        result = {"content": [{"type": "text", "text": f"order {n} placed"}]}
    elif method == "tools/call":
        n, status = sql("SELECT id, status FROM orders ORDER BY id DESC LIMIT 1").split("|")
        if status == "shipped":
            text = f"order {n} already shipped"
        else:
            sql(f"UPDATE orders SET status = 'cancelled' WHERE id = {n}")
            text = f"order {n} cancelled"
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
