import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from resumable_evals.orchestrator import DirEnvironment, Episode
from resumable_evals.recording import Recording

HERE = Path(__file__).parent
HARNESS = [sys.executable, str(HERE / "fake_harness.py"), "{mcp_config}"]
MCP = {"fake": [sys.executable, str(HERE / "fake_mcp_server.py"), "{workdir}/app_data"]}


class Upstream:
    """Stateless scripted model: by default three steps of [incr, clock, note], then end_turn.

    ``script`` gives the tools of each step instead; ``delay`` is the model's latency.
    """

    def __init__(self, script=None, delay=0.0):
        self.hits = 0
        self.status = 200
        self.script = script or [["incr", "clock", "note"]] * 3
        self.delay = delay
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.hits += 1
                time.sleep(up.delay)
                n = len(body["messages"])
                if (n - 1) // 2 < len(up.script):
                    content = [{"type": "tool_use", "id": f"toolu_{n}_{i}", "name": t, "input": {}}
                               for i, t in enumerate(up.script[(n - 1) // 2])]
                    reply = {"content": content, "stop_reason": "tool_use"}
                else:
                    reply = {"content": [{"type": "text", "text": "counted"}], "stop_reason": "end_turn"}
                if up.status != 200:
                    reply = {"type": "error", "error": {"type": "authentication_error", "message": "invalid"}}
                data = json.dumps(reply).encode()
                self.send_response(up.status)
                self.send_header("Content-Type", "application/json")
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
    up = Upstream()
    yield up
    up.httpd.shutdown()


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "base"
    (b / "app_data").mkdir(parents=True)
    return b


def episode(tmp_path, base, upstream, **kw):
    kw.setdefault("attempt_timeout", 30)
    return Episode(dir=tmp_path / "episode", harness=HARNESS, environment=DirEnvironment(base, tmp_path / "work"),
                   mcp_servers=MCP, upstream=upstream.url, **kw)


def crash_once(tmp_path, at_step):
    marker = tmp_path / "crash-marker"
    marker.touch()
    return {"CRASH_MARKER": str(marker), "CRASH_AT_STEP": str(at_step)}


def final_state(tmp_path):
    work = tmp_path / "work"
    return (work / "notes.txt").read_text().splitlines(), (work / "app_data" / "counter").read_text(), (work / "done.txt").read_text()


def test_clean_run(tmp_path, base, upstream):
    meta = episode(tmp_path, base, upstream).run()
    assert meta["status"] == "completed" and not meta["resumed"] and len(meta["attempts"]) == 1
    assert final_state(tmp_path) == (["step 0", "step 1", "step 2"], "3", "counted")
    assert upstream.hits == 4


def test_crash_mid_step_rebuilds_and_resumes(tmp_path, base, upstream):
    meta = episode(tmp_path, base, upstream, env=crash_once(tmp_path, at_step=1)).run()
    assert [a["outcome"] for a in meta["attempts"]] == ["failed", "completed"]
    assert meta["status"] == "completed" and meta["resumed"] and meta["resumed_from_steps"] == [2]
    # Fresh environment: the crashed attempt's half-done step is not doubled.
    assert final_state(tmp_path) == (["step 0", "step 1", "step 2"], "3", "counted")
    assert upstream.hits == 4  # replayed steps were never re-requested
    assert len(Recording(tmp_path / "episode" / "mcp-fake.jsonl").events) == 3


def test_fault_after_reply_recorded_but_not_delivered(tmp_path, base, upstream):
    meta = episode(tmp_path, base, upstream, fault_after_steps=2).run()
    assert meta["attempts"][0]["reason"] == "fault injected (harness) after step 2"
    assert meta["status"] == "completed" and meta["resumed_from_steps"] == [2]
    assert final_state(tmp_path) == (["step 0", "step 1", "step 2"], "3", "counted")
    assert upstream.hits == 4


class DirtyOnRebuild(DirEnvironment):
    """An environment that is not fresh on its second start (state leaked into app_data/)."""

    starts = 0

    def start(self, boot_env):
        rt = super().start(boot_env)
        work = Path(rt.workdir)
        self.starts += 1
        if self.starts == 2:
            (work / "app_data" / "counter").write_text("5")
        return rt


def test_divergence_reruns_from_scratch(tmp_path, base, upstream):
    ep = episode(tmp_path, base, upstream, env=crash_once(tmp_path, at_step=1))
    ep.environment = DirtyOnRebuild(base, tmp_path / "work")
    meta = ep.run()
    assert [a["outcome"] for a in meta["attempts"]] == ["failed", "diverged", "completed"]
    div = meta["attempts"][1]["divergence"]
    assert any(d["recording"] == "mcp-fake.jsonl" for d in div)
    assert meta["status"] == "completed" and meta["reran_after_divergence"] and not meta["resumed"]
    assert (tmp_path / "episode" / "diverged-1" / "mcp-fake.jsonl.diverged.json").exists()
    assert final_state(tmp_path) == (["step 0", "step 1", "step 2"], "3", "counted")


def test_divergence_can_end_the_episode(tmp_path, base, upstream):
    ep = episode(tmp_path, base, upstream, env=crash_once(tmp_path, at_step=1), rerun_on_divergence=False)
    ep.environment = DirtyOnRebuild(base, tmp_path / "work")
    meta = ep.run()
    assert meta["status"] == "diverged" and len(meta["attempts"]) == 2


def test_attempt_timeout_counts_as_failure(tmp_path, base, upstream):
    """A hang: the attempt is retried."""
    ep = episode(tmp_path, base, upstream, max_attempts=2)
    ep.harness, ep.attempt_timeout = [sys.executable, "-c", "import time; time.sleep(30)"], 0.5
    meta = ep.run()
    assert meta["status"] == "failed"
    assert [a["reason"] for a in meta["attempts"]] == ["attempt timeout after 0.5s"] * 2


def test_using_up_the_time_limit_ends_the_episode(tmp_path, base, upstream):
    ep = episode(tmp_path, base, upstream, max_attempts=3)
    ep.harness, ep.timeout = [sys.executable, "-c", "import time; time.sleep(30)"], 0.5
    meta = ep.run()
    assert meta["status"] == "timed_out" and len(meta["attempts"]) == 1  # not retried


def test_a_resumed_episode_gets_no_extra_time(tmp_path, base):
    """Four steps of 0.4 s against a 1.4 s limit: an uninterrupted run times out, and so does a
    resumed one, which before had the whole limit again for its last two steps."""
    slow = Upstream(delay=0.4)
    try:
        straight = episode(tmp_path / "a", base, slow, timeout=1.4).run()
        resumed = episode(tmp_path / "b", base, slow, timeout=1.4, env=crash_once(tmp_path, at_step=1)).run()
    finally:
        slow.httpd.shutdown()
    assert straight["status"] == "timed_out"
    assert [a["outcome"] for a in resumed["attempts"]] == ["failed", "timed_out"], resumed
    events = Recording(tmp_path / "b" / "episode" / "model.jsonl").events
    lives = [e.meta["live"] for e in events]
    assert lives == sorted(lives)  # the second attempt carried on from the first one's time


def test_episode_json_is_saved_before_the_first_attempt(tmp_path, base, upstream):
    class Broken(DirEnvironment):
        def start(self, boot_env):
            raise RuntimeError("orchestrator host lost")
    ep = episode(tmp_path, base, upstream)
    ep.environment = Broken(base, tmp_path / "work")
    with pytest.raises(RuntimeError):
        ep.run()
    meta = json.loads((tmp_path / "episode" / "episode.json").read_text())
    assert meta["seed"] == 0 and meta["started"] and meta["attempts"] == []


def test_a_recording_without_live_time_is_not_resumed_under_a_time_limit(tmp_path, base, upstream):
    Recording(tmp_path / "episode" / "model.jsonl").append(0, "model", {"shape": {}, "body": {}}, {}, t=1.0)
    with pytest.raises(ValueError, match="no live working time"):
        episode(tmp_path, base, upstream, timeout=30).run()


def test_auth_failure_stops_the_episode(tmp_path, base, upstream):
    upstream.status = 401
    meta = episode(tmp_path, base, upstream).run()
    assert meta["status"] == "auth_failed" and len(meta["attempts"]) == 1


def test_resume_with_another_seed_is_refused(tmp_path, base, upstream):
    episode(tmp_path, base, upstream, env=crash_once(tmp_path, 1), max_attempts=1).run()
    assert json.loads((tmp_path / "episode" / "episode.json").read_text())["seed"] == 0
    with pytest.raises(ValueError, match="recorded with seed 0"):
        episode(tmp_path, base, upstream, seed=1).run()


def test_random_shim_refused_on_macos(tmp_path, base, upstream):
    ep = episode(tmp_path, base, upstream, random_lib="/usr/local/lib/librandpin.so")
    ep.environment.platform = "darwin"
    with pytest.raises(ValueError, match="cannot run on macOS"):
        ep.run()


def test_settling_needs_a_container(tmp_path, base, upstream):
    with pytest.raises(ValueError, match="only a container"):
        episode(tmp_path, base, upstream, settle_timeout=30).run()


def test_cli_builds_the_container_environment(tmp_path, monkeypatch):
    from resumable_evals import orchestrator
    seen = {}
    monkeypatch.setattr(orchestrator.Episode, "run", lambda self: seen.setdefault("ep", self) and {"status": "completed"})
    orchestrator.main(["--episode", str(tmp_path / "ep"), "--image", "img", "--workdir", "/w", "--proxy-bind", "172.17.0.1",
                       "--volume", "pgdata=/var/lib/postgresql/data", "--docker-arg=--memory=1g",
                       "--random-lib", "/r.so", "--ready", "pg_isready", "--", "harness"])
    ep = seen["ep"]
    env = ep.environment
    assert (env.image, env.workdir, env.run_args, env.volumes, env.proxy_bind) == (
        "img", "/w", ["--memory=1g"], {"pgdata": "/var/lib/postgresql/data"}, "172.17.0.1")
    assert ep.random_lib == "/r.so" and ep.ready == ["sh", "-c", "pg_isready"] and ep.settle_timeout == 30
    assert ep.harness == ["harness"]
