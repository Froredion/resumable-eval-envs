import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from resumable_evals.clock import Clock
from resumable_evals.model_proxy import ModelProxy
from resumable_evals.recording import Recording

TOKEN = "sk-ant-oat01-test-token-do-not-record"


class FakeUpstream:
    """Anthropic-shaped upstream: numbered replies, SSE when stream=true, scripted failures."""

    def __init__(self):
        self.hits = []
        self.fail_next = []
        self.delay = 0.0
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.hits.append((self.path, dict(self.headers)))
                time.sleep(up.delay)
                if up.fail_next:
                    return self._send(up.fail_next.pop(0), "application/json", b'{"type":"error"}')
                n = len(up.hits)
                if body.get("stream"):
                    data = f"event: message_start\ndata: {{\"n\":{n}}}\n\nevent: message_stop\ndata: {{}}\n\n".encode()
                    return self._send(200, "text/event-stream", data)
                self._send(200, "application/json", json.dumps({"content": [{"text": f"reply{n}"}]}).encode())

            def do_GET(self):
                up.hits.append((self.path, dict(self.headers)))
                self._send(200, "application/json", b'{"data":[]}')

            def _send(self, status, ctype, data):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def upstream():
    up = FakeUpstream()
    yield up
    up.httpd.shutdown()


def start(rec, upstream, clock=None, headers=None):
    proxy = ModelProxy(Recording(rec), upstream.url, clock, headers)
    httpd = proxy.serve()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return proxy, httpd.server_address[1]


def call(port, messages, model="claude-opus-5-5", stream=False, method="POST", path="/v1/messages?beta=true", auth=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps({"model": model, "stream": stream, "messages": messages}) if method == "POST" else None
    headers = auth or {"Authorization": f"Bearer {TOKEN}"}
    conn.request(method, path, body=body, headers=headers | {"Content-Type": "application/json"})
    r = conn.getresponse()
    out = r.status, r.getheader("Content-Type"), r.read()
    conn.close()
    return out


def msgs(n, stamp="t0"):
    return [{"role": "user", "content": f"turn {i} at {stamp}"} for i in range(n)]


def test_record_rebuild_then_live(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    first = [call(port, msgs(1)), call(port, msgs(3), stream=True)]
    proxy.httpd.shutdown()
    assert len(upstream.hits) == 2
    assert upstream.hits[0][1]["Authorization"] == f"Bearer {TOKEN}"  # auth forwarded
    assert TOKEN not in rec.read_text()  # ...never recorded

    # Rebuild: prompts carry different timestamps, replies come from the recording.
    proxy, port = start(rec, upstream)
    again = [call(port, msgs(1, "t9")), call(port, msgs(3, "t9"), stream=True)]
    assert again == first and len(upstream.hits) == 2
    assert again[1][1] == "text/event-stream"

    status, _, live = call(port, msgs(5))  # recording exhausted: live
    assert status == 200 and json.loads(live)["content"][0]["text"] == "reply3"
    assert len(upstream.hits) == 3 and len(Recording(rec).events) == 3
    proxy.httpd.shutdown()


def test_shape_divergence_stops_proxy(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    call(port, msgs(1))
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    status, _, body = call(port, msgs(1), model="claude-haiku-5-5")  # e.g. a side call arrived first
    assert status == 400 and "diverged" in json.loads(body)["error"]["message"]
    report = json.loads((tmp_path / "model.jsonl.diverged.json").read_text())
    assert "claude-opus-5-5" in report["detail"] and "claude-haiku-5-5" in report["detail"]
    assert proxy.diverged and len(upstream.hits) == 1


def test_transient_and_auth_failures_are_not_recorded(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    upstream.fail_next = [529, 401]
    proxy, port = start(rec, upstream)
    assert call(port, msgs(1))[0] == 529
    assert call(port, msgs(1))[0] == 401  # bad credential
    assert call(port, msgs(1))[0] == 200  # harness retry, credential fixed
    proxy.httpd.shutdown()
    assert [e.response["status"] for e in Recording(rec).events] == [200]


def test_client_errors_are_recorded(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    upstream.fail_next = [400]  # e.g. prompt too long: the harness reacts, so the rebuild must too
    proxy, port = start(rec, upstream)
    assert call(port, msgs(1))[0] == 400
    proxy.httpd.shutdown()
    proxy, port = start(rec, upstream)
    assert call(port, msgs(1))[0] == 400 and len(upstream.hits) == 1
    proxy.httpd.shutdown()


def test_other_endpoints_pass_through_unrecorded(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    status, _, body = call(port, None, method="GET", path="/v1/models")
    proxy.httpd.shutdown()
    assert status == 200 and json.loads(body) == {"data": []}
    assert Recording(rec).events == []


def tool_turn(n, result):
    """n messages, the last reporting a tool result (as Claude Code does after running Bash)."""
    m = msgs(n - 1)
    m.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": result}]})
    return m


def test_harness_tool_results_are_checked(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    call(port, tool_turn(3, "3 passed in 0.52s"))
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    assert call(port, tool_turn(3, "3 passed in 0.61s"))[0] == 200  # duration normalized
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    status, _, body = call(port, tool_turn(3, "1 failed, 2 passed in 0.5s"))
    assert status == 400 and "tool results differ" in json.loads(body)["error"]["message"]
    assert proxy.diverged


def test_clock_pinned_to_recorded_time(tmp_path, upstream):
    rec, clock = tmp_path / "model.jsonl", Clock(tmp_path / "clock")
    proxy, port = start(rec, upstream, clock)
    call(port, msgs(1))
    proxy.httpd.shutdown()
    t = Recording(rec).events[0].meta["t"]
    assert clock.path.read_text() == f"{int(t)}\n"

    clock.pin(0)
    proxy, port = start(rec, upstream, clock)
    call(port, msgs(1))
    assert clock.path.read_text() == f"{int(t)}\n"  # rebuild pins the recorded time
    proxy.httpd.shutdown()


def test_proxy_supplies_credentials_harness_never_sees(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream, headers={"Authorization": f"Bearer {TOKEN}", "anthropic-beta": "oauth-2025-04-20"})
    call(port, msgs(1), auth={"x-api-key": "placeholder", "anthropic-beta": "harness-beta"})
    call(port, None, method="GET", path="/v1/models", auth={"x-api-key": "placeholder"})
    proxy.httpd.shutdown()
    for _, headers in upstream.hits:
        lower = {k.lower(): v for k, v in headers.items()}
        assert "x-api-key" not in lower and lower.get("authorization") == f"Bearer {TOKEN}"
    assert {k.lower(): v for k, v in upstream.hits[0][1].items()}["anthropic-beta"] == "harness-beta,oauth-2025-04-20"
    assert TOKEN not in rec.read_text()


def test_tool_results_followed_by_a_harness_message_are_checked(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    nudge = {"role": "system", "content": [{"type": "text", "text": "The user hasn't heard from you in a while"}]}
    proxy, port = start(rec, upstream)
    call(port, tool_turn(3, "alpha") + [nudge])
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    status, _, body = call(port, tool_turn(3, "beta") + [nudge])
    assert status == 400 and "tool results differ" in json.loads(body)["error"]["message"]


# -- parallel conversations (subagents) -------------------------------------

def turn(task, *replies):
    """A conversation: the task, then each earlier reply and a user follow-up."""
    m = [{"role": "user", "content": f"task {task}"}]
    for r in replies:
        m += [{"role": "assistant", "content": json.loads(r)["content"]}, {"role": "user", "content": "go on"}]
    return m


def in_parallel(*calls):
    out = [None] * len(calls)
    threads = [threading.Thread(target=lambda i=i, c=c: out.__setitem__(i, c())) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
        time.sleep(0.05)  # start in this order
    for t in threads:
        t.join()
    return out


def test_parallel_conversations_replay_in_another_order(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    upstream.delay = 0.3  # B arrives while A is in flight
    proxy, port = start(rec, upstream)
    a1, b1 = in_parallel(lambda: call(port, turn("A")), lambda: call(port, turn("B")))
    a2, b2 = in_parallel(lambda: call(port, turn("A", a1[2])), lambda: call(port, turn("B", b1[2])))
    proxy.httpd.shutdown()
    events = Recording(rec).events
    assert [e.meta.get("overlaps_previous", False) for e in events] == [False, True, False, True]

    # Rebuild: the same conversations, each round arriving in the opposite order.
    proxy, port = start(rec, upstream)
    rb1, ra1 = in_parallel(lambda: call(port, turn("B")), lambda: call(port, turn("A")))
    rb2, ra2 = in_parallel(lambda: call(port, turn("B", rb1[2])), lambda: call(port, turn("A", ra1[2])))
    assert (ra1, rb1, ra2, rb2) == (a1, b1, a2, b2)  # each conversation got its own replies
    assert not proxy.diverged and len(upstream.hits) == 4
    proxy.httpd.shutdown()


def test_a_conversation_running_ahead_waits_for_the_other(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    upstream.delay = 0.3
    proxy, port = start(rec, upstream)
    a1, b1 = in_parallel(lambda: call(port, turn("A")), lambda: call(port, turn("B")))
    upstream.delay = 0
    b2 = call(port, turn("B", b1[2]))  # sent after both replies: strictly ordered after them
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    assert call(port, turn("B")) == b1
    # B's next request arrives before A's first; it waits for A instead of diverging.
    rb2, ra1 = in_parallel(lambda: call(port, turn("B", b1[2])), lambda: (time.sleep(0.3), call(port, turn("A")))[1])
    assert (ra1, rb2) == (a1, b2) and not proxy.diverged
    proxy.httpd.shutdown()


def test_answer_from_the_wrong_conversation_diverges(tmp_path, upstream):
    """Same shape, no overlap: the assistant turns show the request belongs elsewhere."""
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    r1 = call(port, turn("A"))
    call(port, turn("A", r1[2]))
    proxy.httpd.shutdown()

    proxy, port = start(rec, upstream)
    call(port, turn("A"))
    other = json.dumps({"content": [{"text": "a reply from another conversation"}]}).encode()
    status, _, body = call(port, turn("A", other))
    assert status == 400 and "assistant turns differ" in json.loads(body)["error"]["message"]
    assert proxy.diverged


def test_settle_timeout_is_noted_and_the_replay_carries_on(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    proxy.processes = lambda: ["harness"]
    first = call(port, msgs(1))
    proxy.httpd.shutdown()
    assert Recording(rec).events[0].meta["processes"] == ["harness"]

    proxy, port = start(rec, upstream)
    proxy.processes = lambda: ["harness", "server --port 8000"]  # never exits
    proxy.settle_timeout = 0.3
    assert call(port, msgs(1)) == first
    assert proxy.settle_timeouts == [{"step": 0, "still_running": ["server --port 8000"]}]
    proxy.httpd.shutdown()


def test_only_listed_endpoints_are_passed_through(tmp_path, upstream):
    """The proxy adds the credential, so it forwards only what the harness needs."""
    proxy, port = start(tmp_path / "model.jsonl", upstream, headers={"Authorization": f"Bearer {TOKEN}"})
    proxy.passthrough = frozenset({("GET", "/v1/models")})
    assert call(port, None, method="GET", path="/v1/models?limit=5")[0] == 200
    status, _, body = call(port, None, method="GET", path="/v1/files")
    assert status == 404 and "not passed through" in json.loads(body)["error"]["message"]
    assert [path for path, _ in upstream.hits] == ["/v1/models?limit=5"]  # the refused one never left
    assert proxy.forwarded == {"GET /v1/models"} and proxy.refused == {"GET /v1/files"}
    proxy.httpd.shutdown()


def test_replay_is_not_charged_to_the_time_limit(tmp_path, upstream):
    rec = tmp_path / "model.jsonl"
    proxy, port = start(rec, upstream)
    proxy.start_live_clock()
    r1 = call(port, turn("A"))
    call(port, turn("A", r1[2]))
    proxy.httpd.shutdown()
    base = Recording(rec).events[-1].meta["live"]

    proxy, port = start(rec, upstream)
    proxy.start_live_clock()  # something left to replay: not counting yet
    time.sleep(0.5)
    call(port, turn("A"))
    time.sleep(0.5)
    r2 = call(port, turn("A", r1[2]))  # the last replayed reply: counting from here
    time.sleep(0.3)
    call(port, turn("A", r1[2], r2[2]))
    live = Recording(rec).events[-1].meta["live"]
    assert base + 0.3 <= live < base + 0.6
    proxy.httpd.shutdown()
