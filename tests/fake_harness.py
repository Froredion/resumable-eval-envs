"""Minimal agent harness for orchestrator tests.

Talks to the model at $ANTHROPIC_BASE_URL and runs the tools the model asks
for: ``incr`` through the MCP server named ``fake`` in the MCP config given
as argv[1], and two tools of its own, like Claude Code's Bash and Write:
``clock`` reads the clock (``date`` when libfaketime is preloaded, else the
pinned clock file) and ``note`` appends to notes.txt in the working
directory. Crashes once, partway through a step, if $CRASH_MARKER
names an existing file and the step is $CRASH_AT_STEP.
"""

import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


class Mcp:
    def __init__(self, spec):
        self.proc = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=os.environ | spec.get("env", {}))
        self.n = 0
        self.request("initialize", {})

    def request(self, method, params):
        self.n += 1
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"error": {"message": "MCP server closed"}}
            msg = json.loads(line)
            if msg.get("id") == self.n:
                return msg

    def call(self, name):
        msg = self.request("tools/call", {"name": name, "arguments": {}})
        if "error" in msg:
            return msg["error"]["message"], True
        return msg["result"]["content"][0]["text"], False


def model(messages):
    u = urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    conn.request("POST", "/v1/messages", json.dumps({"model": "fake-model", "messages": messages}),
                 {"Content-Type": "application/json"})
    r = conn.getresponse()
    body = json.loads(r.read())
    if r.status != 200:
        sys.exit(f"model error {r.status}: {body}")
    return body


mcp = Mcp(json.loads(Path(sys.argv[1]).read_text())["mcpServers"]["fake"])
crash_marker = os.environ.get("CRASH_MARKER")
messages = [{"role": "user", "content": "count"}]
step = 0
while True:
    reply = model(messages)
    messages.append({"role": "assistant", "content": reply["content"]})
    if reply["stop_reason"] == "end_turn":
        break
    results = []
    for block in reply["content"]:
        if block["type"] != "tool_use":
            continue
        error = False
        if block["name"] == "incr":
            text, error = mcp.call("incr")
        elif block["name"] == "clock":
            if "LD_PRELOAD" in os.environ:
                text = subprocess.run(["date", "-u", "+%s"], capture_output=True, text=True).stdout
            else:
                text = Path(os.environ["FAKETIME_TIMESTAMP_FILE"]).read_text()
        elif block["name"] == "note":
            with open("notes.txt", "a") as f:
                f.write(f"step {step}\n")
            text = f"{len(Path('notes.txt').read_text().splitlines())} notes"
        results.append({"type": "tool_result", "tool_use_id": block["id"], "content": text, "is_error": error})
        if crash_marker and os.path.exists(crash_marker) and step == int(os.environ["CRASH_AT_STEP"]):
            os.remove(crash_marker)
            os._exit(1)  # mid-step: some of this step's tools ran, the model never hears about them
    messages.append({"role": "user", "content": results})
    step += 1
Path("done.txt").write_text(reply["content"][0]["text"])
