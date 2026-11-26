import json
import subprocess
import sys
import time
from pathlib import Path

from resumable_evals.mcp_proxy import DIVERGED_EXIT
from resumable_evals.recording import Recording

SERVER = Path(__file__).with_name("fake_mcp_server.py")


def init(i=0):
    return [
        {"jsonrpc": "2.0", "id": f"init{i}", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def tool(id, name, meta=None, **arguments):
    params = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params}


def session(rec, state, messages):
    """Run one harness session through the proxy against a server whose state dir is `state`."""
    state.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "resumable_evals.mcp_proxy", "--recording", str(rec), "--", sys.executable, str(SERVER), str(state)],
        input="".join(json.dumps(m) + "\n" for m in messages).encode(),
        capture_output=True,
        timeout=30,
    )
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    return proc.returncode, replies


def text(reply):
    return reply["result"]["content"][0]["text"]


def test_record_rebuild_then_live(tmp_path):
    rec = tmp_path / "ep" / "fake.jsonl"
    code, r = session(rec, tmp_path / "run1", init() + [tool(1, "incr"), tool(2, "incr")])
    assert code == 0 and [text(r[1]), text(r[2])] == ["1", "2"]
    assert len(Recording(rec).events) == 2

    # Fresh environment, harness restarts and replays both calls, then makes a new one.
    code, r = session(rec, tmp_path / "run2", init() + [tool(1, "incr"), tool(2, "incr"), tool(3, "incr", by=10)])
    assert code == 0 and [text(r[1]), text(r[2]), text(r[3])] == ["1", "2", "12"]
    events = Recording(rec).events
    assert len(events) == 3 and events[2].request == {"name": "incr", "arguments": {"by": 10}}
    assert not rec.with_name("fake.jsonl.diverged.json").exists()


def test_result_divergence_stops_rebuild(tmp_path):
    rec = tmp_path / "fake.jsonl"
    session(rec, tmp_path / "run1", init() + [tool(1, "incr")])
    dirty = tmp_path / "run2"
    dirty.mkdir()
    (dirty / "counter").write_text("5")  # environment not fresh
    code, r = session(rec, dirty, init() + [tool(1, "incr"), tool(2, "incr")])
    assert code == DIVERGED_EXIT
    assert "diverged" in r[1]["error"]["message"] and 2 not in r
    report = json.loads((tmp_path / "fake.jsonl.diverged.json").read_text())
    assert report["recorded_response"]["result"]["content"][0]["text"] == "1"
    assert report["response"]["result"]["content"][0]["text"] == "6"
    assert len(Recording(rec).events) == 1  # nothing appended


def test_request_divergence_is_not_executed(tmp_path):
    rec = tmp_path / "fake.jsonl"
    session(rec, tmp_path / "run1", init() + [tool(1, "incr")])
    code, r = session(rec, tmp_path / "run2", init() + [tool(1, "incr", by=2)])
    assert code == DIVERGED_EXIT and "request differs" in r[1]["error"]["message"]
    assert not (tmp_path / "run2" / "counter").exists()


def test_meta_does_not_identify_a_call(tmp_path):
    rec = tmp_path / "fake.jsonl"
    session(rec, tmp_path / "run1", init() + [tool(1, "echo", meta={"progressToken": "a"}, text="hi")])
    code, r = session(rec, tmp_path / "run2", init() + [tool(1, "echo", meta={"progressToken": "b"}, text="hi")])
    assert code == 0 and text(r[1]) == "hi"
    assert Recording(rec).events[0].request == {"name": "echo", "arguments": {"text": "hi"}}


def test_non_tool_messages_pass_through_unrecorded(tmp_path):
    rec = tmp_path / "fake.jsonl"
    code, r = session(rec, tmp_path / "run1", init() + [{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}])
    assert code == 0 and r["init0"]["result"]["serverInfo"]["name"] == "fake"
    assert [t["name"] for t in r[9]["result"]["tools"]] == ["incr", "echo"]
    assert Recording(rec).events == []


def test_listener_refuses_a_second_connection(tmp_path):
    import socket
    from resumable_evals.mcp_proxy import McpListener
    (tmp_path / "data").mkdir()
    listener = McpListener(tmp_path / "mcp.jsonl", ([sys.executable, str(SERVER), str(tmp_path / "data")], None, None), "127.0.0.1")
    first = socket.create_connection(("127.0.0.1", listener.port))
    f = first.makefile("rwb")
    f.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"incr","arguments":{}}}\n')
    f.flush()
    assert json.loads(f.readline())["id"] == 1
    second = socket.create_connection(("127.0.0.1", listener.port))
    assert second.recv(1) == b""  # closed at once
    for _ in range(50):
        if listener.refused:
            break
        time.sleep(0.02)
    assert listener.refused == 1
    first.close()
    listener.stop()
    assert len(Recording(tmp_path / "mcp.jsonl").events) == 1
