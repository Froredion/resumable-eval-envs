"""MCP proxy for stdio servers.

The harness launches this instead of the MCP server, one proxy per server and
one recording file per proxy::

    python -m resumable_evals.mcp_proxy --recording ep/fs.jsonl -- fs-server --root /work

Every message is passed through unchanged except ``tools/call``, which is
executed one at a time in arrival order (in the original run too, so the
order that was recorded is the order the environment saw).

There is no mode flag. While the recording still has tool calls left the
proxy is rebuilding: each call must equal the next recorded request (else it
is refused before reaching the server) and its result must match the
recorded result. Once the recording is exhausted the proxy is live and
appends each call, fsynced, before the reply reaches the harness.

On divergence the harness gets a JSON-RPC error for that call, details are
written to ``<recording>.diverged.json``, the server is stopped and the proxy
exits with status 3. See docs/DESIGN.md.

The orchestrator runs it on the host, behind a :class:`McpListener`: the
harness inside the environment launches ``relay.py``, which connects to the
listener, and the real server is started inside the environment with
``docker exec``. The recording never enters the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

from .rebuild import Diverged, RebuildCursor
from .recording import Recording

DIVERGED_EXIT = 3


def tool_request(params: Any) -> dict[str, Any]:
    """What identifies a tool call: its params minus ``_meta`` (progress tokens and other per-session ids)."""
    return {k: v for k, v in (params or {}).items() if k != "_meta"}


def tool_response(msg: dict[str, Any]) -> dict[str, Any]:
    return {k: msg[k] for k in ("result", "error") if k in msg}


def _parse(line: bytes) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class StdioProxy:
    def __init__(self, recording: Recording, server_cmd: list[str], client_in: BinaryIO, client_out: BinaryIO,
                 env: dict[str, str] | None = None, cwd: str | None = None):
        self.recording = recording
        self.cursor = RebuildCursor(recording)
        self.client_in = client_in
        self.client_out = client_out
        self.server = subprocess.Popen(server_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=cwd)
        self.diverged: Diverged | None = None
        self._tool_count = len(recording.of_kind("tool"))
        self._calls: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._inflight: dict[str, Any] | None = None
        self._done = threading.Event()
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._server_lock = threading.Lock()

    @property
    def diverged_path(self) -> Path:
        return self.recording.path.with_name(self.recording.path.name + ".diverged.json")

    def run(self) -> int:
        for target in (self._pump_client, self._pump_server, self._dispatch):
            threading.Thread(target=target, daemon=True).start()
        self._stop.wait()
        if self.server.poll() is None:
            self._close_server_stdin()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        if self.diverged:
            return DIVERGED_EXIT
        return self.server.returncode or 0

    # -- plumbing ---------------------------------------------------------

    def _to_client(self, data: bytes | dict[str, Any]) -> None:
        if isinstance(data, dict):
            data = json.dumps(data, separators=(",", ":")).encode() + b"\n"
        with self._client_lock:
            try:
                self.client_out.write(data)
                self.client_out.flush()
            except OSError:
                pass  # client gone (harness killed); a reply already recorded is the step the design may lose

    def _to_server(self, data: bytes | dict[str, Any]) -> None:
        if isinstance(data, dict):
            data = json.dumps(data, separators=(",", ":")).encode() + b"\n"
        with self._server_lock:
            try:
                self.server.stdin.write(data)
                self.server.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass  # server gone; _pump_server sees EOF and stops

    def _close_server_stdin(self) -> None:
        with self._server_lock:
            try:
                self.server.stdin.close()
            except OSError:
                pass

    def _pump_client(self) -> None:
        try:
            for line in self.client_in:
                msg = _parse(line)
                if msg is not None and msg.get("method") == "tools/call" and "id" in msg:
                    self._calls.put(msg)
                else:
                    self._to_server(line)
        except OSError:
            pass  # the client's connection was reset (the harness was killed): same as it closing
        self._calls.put(None)  # client closed: finish queued calls, then close the server

    def _pump_server(self) -> None:
        for line in self.server.stdout:
            msg = _parse(line)
            call = self._inflight
            if (
                call is not None
                and msg is not None
                and "method" not in msg
                and msg.get("id") == call["id"]
                and ("result" in msg or "error" in msg)
            ):
                self._finish_call(call, msg, line)
                self._inflight = None
                self._done.set()
            else:
                self._to_client(line)
        self._done.set()
        self._stop.set()

    # -- tool calls -------------------------------------------------------

    def _dispatch(self) -> None:
        while (call := self._calls.get()) is not None:
            try:
                self.cursor.expect_tool(tool_request(call.get("params")))
            except Diverged as d:
                self._diverge(call, d)
                return
            self._done.clear()
            self._inflight = call
            self._to_server(call)
            self._done.wait()
            if self.diverged or self._stop.is_set():
                return
        self._close_server_stdin()

    def _finish_call(self, call: dict[str, Any], reply: dict[str, Any], line: bytes) -> None:
        request = tool_request(call.get("params"))
        response = tool_response(reply)
        if self.cursor.next_tool_call() is not None:
            try:
                self.cursor.check_tool(request, response)
            except Diverged as d:
                self._diverge(call, d, response)
                return
        else:
            self.recording.append(self._tool_count, "tool", request, response)
            self._tool_count += 1
        self._to_client(line)

    def _diverge(self, call: dict[str, Any], d: Diverged, response: Any = None) -> None:
        self.diverged = d
        report = {
            "seq": d.event.seq,
            "step": d.event.step,
            "recorded_request": d.event.request,
            "recorded_response": d.event.response,
            "request": tool_request(call.get("params")),
            "response": response,
            "detail": str(d.mismatch),
        }
        with self.diverged_path.open("w") as f:
            json.dump(report, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        print(f"resumable-evals: rebuild {d}", file=sys.stderr)
        self._to_client(
            {"jsonrpc": "2.0", "id": call["id"], "error": {"code": -32000, "message": f"resumable-evals: rebuild {d}"}}
        )
        self.server.terminate()
        self._stop.set()


class McpListener:
    """One MCP server's proxy, on the host, for one attempt.

    Listens on ``bind`` (a free port: :attr:`port`); the first connection,
    from ``relay.py`` in the environment, gets a :class:`StdioProxy` on
    ``recording_path`` with the server started by ``server`` (argv, process
    env, cwd: a command that runs it inside the environment). A second
    connection is refused and counted in :attr:`refused`: its proxy would
    start the recording over from the first call.
    """

    def __init__(self, recording_path: Path, server: tuple[list[str], dict[str, str], str | None], bind: str):
        self.recording_path = recording_path
        self.server = server
        self.sock = socket.create_server((bind, 0))
        self.port: int = self.sock.getsockname()[1]
        self.refused = 0
        self.proxy: StdioProxy | None = None
        self._conn: socket.socket | None = None
        self._serving: threading.Thread | None = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # stopped
            if self.proxy is not None:
                self.refused += 1
                conn.close()
                continue
            argv, env, cwd = self.server
            self._conn = conn
            self.proxy = StdioProxy(Recording(self.recording_path), argv, conn.makefile("rb"), conn.makefile("wb"), env, cwd)
            self._serving = threading.Thread(target=self._serve, daemon=True)
            self._serving.start()

    def _serve(self) -> None:
        assert self.proxy is not None and self._conn is not None
        try:
            self.proxy.run()
        finally:
            self._conn.close()

    def stop(self) -> None:
        """Close the connection and wait for the proxy, so the recording is final."""
        self.sock.close()
        if self._conn is not None:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)  # the proxy sees the client go
            except OSError:
                pass
        if self._serving is not None:
            self._serving.join(15)
            if self._serving.is_alive() and self.proxy is not None:
                self.proxy.server.kill()
                self._serving.join(15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resumable_evals.mcp_proxy", description=__doc__.splitlines()[0])
    parser.add_argument("--recording", required=True, type=Path, help="JSONL recording for this server")
    parser.add_argument("server", nargs=argparse.REMAINDER, help="-- server command and arguments")
    args = parser.parse_args(argv)
    cmd = args.server[1:] if args.server[:1] == ["--"] else args.server
    if not cmd:
        parser.error("missing server command after --")
    proxy = StdioProxy(Recording(args.recording), cmd, sys.stdin.buffer, sys.stdout.buffer)
    return proxy.run()


if __name__ == "__main__":
    sys.exit(main())
