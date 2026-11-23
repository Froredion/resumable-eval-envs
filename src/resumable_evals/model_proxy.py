"""Model API proxy for the Anthropic Messages API (Claude Code first).

Point the harness at it, e.g. for Claude Code::

    python -m resumable_evals.model_proxy --recording ep/model.jsonl --port 8787
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude ...

``POST /v1/messages`` is the model call. Everything else (token counting,
profile and model listing) is passed through to the upstream unrecorded.

There is no mode flag. While the recording has model replies left, each
request is answered from it, in order, without contacting the upstream; the
harness fast-forwards. Replies are matched by order, not content (harnesses
put timestamps and ids in prompts), but the request's *shape* (model, stream
flag, number of messages) must equal the recorded one, which catches
reordered side calls and a harness that has already gone a different way.
On a shape mismatch the harness gets a non-retryable 400, details go to
``<recording>.diverged.json`` and the proxy stops with status 3.

Once the recording is exhausted the proxy is live: it forwards the request,
and appends request and full reply (fsynced) before the reply reaches the
harness. To make that ordering hold, streamed replies are buffered and sent
whole rather than relayed incrementally. Transient failures (408, 429, 5xx)
are not recorded: the harness retries them with the same request, so the
rebuild needs only the reply to the retry.

Auth headers (``x-api-key``, OAuth ``Authorization: Bearer``) are forwarded
and never recorded; a rebuild does not need them until it goes live.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .rebuild import Diverged
from .rebuild import RebuildCursor
from .recording import Recording

DIVERGED_EXIT = 3
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
Response = tuple[int, list[tuple[str, str]], bytes]


def shape(body: dict[str, Any]) -> dict[str, Any]:
    """The parts of a request that must replay identically; content may vary."""
    return {
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "messages": len(body.get("messages") or []),
    }


def transient(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _error(status: int, message: str, kind: str = "api_error") -> Response:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
    return status, [("Content-Type", "application/json")], body


class ModelProxy:
    def __init__(self, recording: Recording, upstream: str = "https://api.anthropic.com"):
        self.recording = recording
        self.cursor = RebuildCursor(recording)
        u = urlsplit(upstream)
        self._https = u.scheme == "https"
        self._netloc = u.netloc
        self._base = u.path.rstrip("/")
        self._lock = threading.Lock()
        self._steps = len(recording.of_kind("model"))
        self.diverged: Diverged | None = None
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def diverged_path(self) -> Path:
        return self.recording.path.with_name(self.recording.path.name + ".diverged.json")

    def forward(self, method: str, path: str, headers: Any, body: bytes | None) -> Response:
        skip = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
        out = {k: v for k, v in headers.items() if k.lower() not in skip}
        out["Accept-Encoding"] = "identity"  # store and replay plain text
        conn_cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = conn_cls(self._netloc, timeout=600)
        try:
            conn.request(method, self._base + path, body=body, headers=out)
            resp = conn.getresponse()
            data = resp.read()
            keep = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}]
            return resp.status, keep, data
        except OSError as e:
            return _error(502, f"resumable-evals: upstream unreachable: {e}")
        finally:
            conn.close()

    def messages(self, path: str, headers: Any, raw: bytes) -> Response:
        try:
            body = json.loads(raw)
        except ValueError:
            return self.forward("POST", path, headers, raw)
        with self._lock:
            if self.diverged:
                return _error(400, f"resumable-evals: rebuild {self.diverged}", "invalid_request_error")
            event = self.cursor.peek_model()
            if event is not None:
                if event.request["shape"] != shape(body):
                    return self._diverge(event, body)
                reply = self.cursor.next_model_reply()
                return reply["status"], [("Content-Type", reply["content_type"])], reply["body"].encode()
            status, out, data = self.forward("POST", path, headers, raw)
            if not transient(status):
                content_type = next((v for k, v in out if k.lower() == "content-type"), "application/json")
                self.recording.append(
                    self._steps,
                    "model",
                    {"path": path, "shape": shape(body), "body": body},
                    {"status": status, "content_type": content_type, "body": data.decode()},
                )
                self._steps += 1
            return status, out, data

    def _diverge(self, event: Any, body: dict[str, Any]) -> Response:
        d = Diverged(event, f"request shape differs: recorded {event.request['shape']}, got {shape(body)}")
        self.diverged = d
        with self.diverged_path.open("w") as f:
            json.dump({"seq": event.seq, "step": event.step, "recorded_shape": event.request["shape"],
                       "shape": shape(body), "detail": str(d)}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        print(f"resumable-evals: rebuild {d}", file=sys.stderr)
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        return _error(400, f"resumable-evals: rebuild {d}", "invalid_request_error")

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else None
                if self.command == "POST" and urlsplit(self.path).path == "/v1/messages":
                    status, out, data = proxy.messages(self.path, self.headers, raw or b"")
                else:
                    status, out, data = proxy.forward(self.command, self.path, self.headers, raw)
                self.send_response(status)
                for k, v in out:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        return self.httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resumable_evals.model_proxy", description=__doc__.splitlines()[0])
    parser.add_argument("--recording", required=True, type=Path, help="JSONL recording for this episode's model calls")
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    proxy = ModelProxy(Recording(args.recording), args.upstream)
    httpd = proxy.serve(args.host, args.port)
    print(f"resumable-evals: model proxy on http://{args.host}:{httpd.server_address[1]}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return DIVERGED_EXIT if proxy.diverged else 0


if __name__ == "__main__":
    sys.exit(main())
