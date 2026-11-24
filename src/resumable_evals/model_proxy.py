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

The tool results in the request's last message must also match the recorded
ones (after normalization). This is the per-step check for tools the harness
runs itself, such as Claude Code's Bash and Write, which never pass through
the MCP proxy. So must the request's assistant turns: they are the harness's
rendering of recorded replies, identical on a correct replay, so a request
answered from the wrong recorded event (a reordered side call, or another
subagent's conversation) is caught even when its shape matches. On any
mismatch the harness gets a non-retryable 400, details go to
``<recording>.diverged.json`` and the proxy stops with status 3.

Requests that arrive while another is in flight (parallel subagents) are
answered one at a time, as always, and recorded with ``overlaps_previous``.
On rebuild such a group may arrive in another order, so a request is
answered from the event in its group that it matches (shape, assistant
turns and tool results; among several that fit, the one with the same
messages); outside a group the order is strict. A request
that matches a later event (one subagent running ahead of another) waits,
up to ``overlap_wait`` seconds, for the events before it to be answered. A
state probe is not run for an overlapping request: other subagents may be
mid-step.

With a ``state_probe``, the environment's state is also checked at every
step: the probe runs before each model request is answered (so it sees the
effects of everything the previous step did, tool calls and the harness's
own commands alike), its result is recorded with the step, and on rebuild
it must match. That catches side effects no tool result shows, such as a
database row written with a different value.

With a ``processes`` lister (the environment's running processes), the
list is recorded with each reply as it comes back from upstream: it shows
which background jobs the agent started had finished by then. On rebuild,
a reply is held until every process not on its recorded list has exited,
up to ``settle_timeout`` seconds, so the agent's next tools see the jobs
done, as they did in the original run, however much faster the replay
is. A timeout is noted in ``settle_timeouts`` and the replay carries on;
the result checks still decide.

With a :class:`~resumable_evals.clock.Clock`, each reply pins the
environment's clock: to now when live (recorded with the event), to the
recorded time on rebuild.

Once the recording is exhausted the proxy is live: it forwards the request,
and appends request and full reply (fsynced) before the reply reaches the
harness. To make that ordering hold, streamed replies are buffered and sent
whole rather than relayed incrementally. Transient failures (408, 429, 5xx)
are not recorded: the harness retries them with the same request, so the
rebuild needs only the reply to the retry. Auth failures (401, 403) are not
recorded either: they depend on the credential, not on the episode.

Auth headers (``x-api-key``, OAuth ``Authorization: Bearer``) are never
recorded; a rebuild does not need them until it goes live. By default the
harness's own are forwarded. With ``upstream_headers`` the proxy drops the
harness's and adds its own instead, so the credential never enters the eval
environment (where the agent could read it); the harness then only needs a
placeholder key.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .clock import Clock
from .compare import compare
from .rebuild import Diverged, RebuildCursor
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


def tool_results(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool results the harness is reporting in this request.

    They are in the messages after the model's last turn; usually one user
    message, but a harness can add its own after it (Claude Code appends a
    system message when a step has taken a while), so all of them count.
    """
    messages = body.get("messages") or []
    i = len(messages)
    while i > 0 and not (isinstance(messages[i - 1], dict) and messages[i - 1].get("role") == "assistant"):
        i -= 1
    out = []
    for message in messages[i:]:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            out += [
                {"content": b.get("content"), "is_error": bool(b.get("is_error"))}
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
    return out


def assistant_turns(body: dict[str, Any]) -> list[Any]:
    """The conversation's earlier model turns, as the harness sends them back."""
    return [m.get("content") for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") == "assistant"]


def mismatch(recorded: dict[str, Any], body: dict[str, Any]) -> str | None:
    """Why ``body`` cannot be answered from the recorded request, or None if it can."""
    if recorded["shape"] != shape(body):
        return f"request shape differs: recorded {recorded['shape']}, got {shape(body)}"
    m = compare(assistant_turns(recorded["body"]), assistant_turns(body))
    if m:
        return f"assistant turns differ: {m}"
    m = compare(tool_results(recorded["body"]), tool_results(body))
    if m:
        return f"tool results differ: {m}"
    return None


def state_differences(recorded: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Names of state probes whose result differs from the recording."""
    names = sorted(set(recorded) | set(state))
    return [n for n in names if (recorded.get(n) or {}).get("sha256") != (state.get(n) or {}).get("sha256")
            or (recorded.get(n) or {}).get("exit") != (state.get(n) or {}).get("exit")]


def unrecorded(status: int) -> bool:
    """Failures that say nothing about the episode: transient ones the harness
    retries, and auth failures that depend on the credential. Replaying either
    would repeat a failure the rebuild has no reason to repeat."""
    return status in (401, 403, 408, 429) or status >= 500


def _error(status: int, message: str, kind: str = "api_error") -> Response:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
    return status, [("Content-Type", "application/json")], body


class ModelProxy:
    def __init__(
        self,
        recording: Recording,
        upstream: str = "https://api.anthropic.com",
        clock: Clock | None = None,
        upstream_headers: dict[str, str] | None = None,
    ):
        self.recording = recording
        self.clock = clock
        self.upstream_headers = upstream_headers or {}
        self.state_probe: Callable[[], dict[str, Any]] | None = None  # see the module docstring
        self.processes: Callable[[], list[str]] | None = None  # see the module docstring
        self.settle_timeout = 30.0
        self.settle_timeouts: list[dict[str, Any]] = []
        self.on_record: Callable[[int], None] | None = None  # called with the step count after each live reply is recorded
        self.cursor = RebuildCursor(recording)
        u = urlsplit(upstream)
        self._https = u.scheme == "https"
        self._netloc = u.netloc
        self._base = u.path.rstrip("/")
        self._lock = threading.Condition()
        self.overlap_wait = 60.0  # see the module docstring
        self._last_sent = float("-inf")  # monotonic time the last model reply was handed back
        self._steps = len(recording.of_kind("model"))
        self.diverged: Diverged | None = None
        self.auth_failed: int | None = None  # status of the last upstream auth failure on a model call
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def steps(self) -> int:
        """Model steps in the recording so far, rebuilt and live."""
        return self._steps

    @property
    def diverged_path(self) -> Path:
        return self.recording.path.with_name(self.recording.path.name + ".diverged.json")

    def forward(self, method: str, path: str, headers: Any, body: bytes | None) -> Response:
        skip = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
        if self.upstream_headers:
            skip |= {"x-api-key", "authorization"}
        out = {k: v for k, v in headers.items() if k.lower() not in skip}
        for k, v in self.upstream_headers.items():
            mine = next((h for h in out if h.lower() == k.lower()), None)
            if mine and k.lower() == "anthropic-beta":
                out[mine] = f"{out[mine]},{v}"  # keep the harness's betas
            else:
                out.pop(mine, None)
                out[k] = v
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
        arrived = time.monotonic()
        deadline = arrived + self.overlap_wait
        with self._lock:
            while (response := self._messages(path, headers, raw, body, arrived, time.monotonic() < deadline)) is None:
                self._lock.wait(max(0.0, deadline - time.monotonic()))
            self._last_sent = time.monotonic()
            self._lock.notify_all()
            return response

    def _messages(self, path: str, headers: Any, raw: bytes, body: dict[str, Any], arrived: float,
                  may_wait: bool) -> Response | None:
        """Answer a model request; None means wait for an earlier one first."""
        if self.diverged:
            return _error(400, f"resumable-evals: rebuild {self.diverged}", "invalid_request_error")
        # Sent before the previous reply was back: another conversation's request (subagents).
        overlaps = arrived < self._last_sent
        window = self.cursor.model_window()
        if window:
            checked = [(e, mismatch(e.request, body)) for e in window]
            fits = [e for e, why in checked if why is None]
            # Several fit (subagents' first requests have no turns to tell them apart):
            # prefer the one whose messages are the same, which a subagent's prompt usually is.
            event = next((e for e in fits if compare(e.request["body"].get("messages"), body.get("messages")) is None),
                         fits[0] if fits else None)
            if event is None:
                later = [e for e in self.cursor.unanswered_models() if e not in window]
                if may_wait and any(mismatch(e.request, body) is None for e in later):
                    return None  # running ahead of another conversation; its request will come
                first, why = checked[0]
                if len(checked) > 1:
                    why = f"matches none of {len(checked)} overlapping recorded requests; the first: {why}"
                return self._diverge(first, why)
            if self.state_probe and "state" in event.meta:
                state = self.state_probe()
                differing = state_differences(event.meta["state"], state)
                if differing:
                    return self._diverge(event, f"environment state differs: {', '.join(differing)}",
                                         {"recorded_state": event.meta["state"], "state": state})
            if self.processes and "processes" in event.meta:
                self._settle(event)
            reply = self.cursor.take_model(event)
            if self.clock and "t" in event.meta:
                self.clock.pin(event.meta["t"])
            return reply["status"], [("Content-Type", reply["content_type"])], reply["body"].encode()
        state = self.state_probe() if self.state_probe and not overlaps else None
        status, out, data = self.forward("POST", path, headers, raw)
        self.auth_failed = status if status in (401, 403) else None
        if not unrecorded(status):
            processes = self.processes() if self.processes and not overlaps else None
            t = time.time()
            content_type = next((v for k, v in out if k.lower() == "content-type"), "application/json")
            self.recording.append(
                self._steps,
                "model",
                {"path": path, "shape": shape(body), "body": body},
                {"status": status, "content_type": content_type, "body": data.decode()},
                t=t,
                **({"state": state} if state is not None else {}),
                **({"processes": processes} if processes is not None else {}),
                **({"overlaps_previous": True} if overlaps else {}),
            )
            self._steps += 1
            if self.clock:
                self.clock.pin(t)
            if self.on_record:
                self.on_record(self._steps)
        return status, out, data

    def _settle(self, event: Any) -> None:
        """Hold the reply until no process runs that had exited by this point in the original run."""
        recorded = Counter(event.meta["processes"])
        deadline = time.monotonic() + self.settle_timeout
        while extra := Counter(self.processes()) - recorded:
            if time.monotonic() > deadline:
                self.settle_timeouts.append({"step": event.step, "still_running": sorted(extra.elements())})
                return
            time.sleep(0.2)

    def _diverge(self, event: Any, detail: str, extra: dict[str, Any] | None = None) -> Response:
        d = Diverged(event, detail)
        self.diverged = d
        with self.diverged_path.open("w") as f:
            json.dump({"seq": event.seq, "step": event.step, "detail": detail} | (extra or {}), f, indent=2)
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

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                    return  # harness went away (crashed or killed); nothing to report
                super().handle_error(request, client_address)

        self.httpd = Server((host, port), Handler)
        return self.httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resumable_evals.model_proxy", description=__doc__.splitlines()[0])
    parser.add_argument("--recording", required=True, type=Path, help="JSONL recording for this episode's model calls")
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--clock-file", type=Path, help="libfaketime timestamp file to pin at each step")
    args = parser.parse_args(argv)
    clock = Clock(args.clock_file) if args.clock_file else None
    proxy = ModelProxy(Recording(args.recording), args.upstream, clock)
    httpd = proxy.serve(args.host, args.port)
    print(f"resumable-evals: model proxy on http://{args.host}:{httpd.server_address[1]}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return DIVERGED_EXIT if proxy.diverged else 0


if __name__ == "__main__":
    sys.exit(main())
