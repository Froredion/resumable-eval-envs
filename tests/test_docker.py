"""End-to-end in Docker: the environment (container) dies mid-episode and is rebuilt from its image."""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from resumable_evals.environments import DockerEnvironment
from resumable_evals.orchestrator import Episode
from resumable_evals.recording import Recording

from test_orchestrator import Upstream

HERE = Path(__file__).parent
IMAGE = "resumable-evals-test:latest"


def docker_ok():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason="Docker not available")


@pytest.fixture(scope="session")
def image():
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, "-f", str(HERE / "docker" / "Dockerfile"), str(HERE)],
                   check=True, capture_output=True)
    return IMAGE


@pytest.fixture
def shared_dir():
    # Docker Desktop for macOS only shares some host paths; pytest's tmp_path (/var/folders) is not one of them.
    d = Path.home() / ".cache" / "resumable-evals-tests" / uuid.uuid4().hex[:8]
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def upstream():
    up = Upstream()
    yield up
    up.httpd.shutdown()


def test_container_killed_mid_episode_is_rebuilt(image, shared_dir, upstream):
    env = DockerEnvironment(image, f"resumable-test-{shared_dir.name}")
    ep = Episode(
        dir=shared_dir / "episode",
        harness=["python", "/opt/eval/fake_harness.py", "{mcp_config}"],
        environment=env,
        mcp_servers={"fake": ["python", "/opt/eval/fake_mcp_server.py", "{workdir}/app_data"]},
        upstream=upstream.url,
        faketime_lib="/usr/local/lib/libfaketime.so.1",
        timeout=120,
        fault_after_steps=2,
        fault="environment",
    )
    try:
        meta = ep.run()
        assert [a["outcome"] for a in meta["attempts"]] == ["failed", "completed"], meta
        assert meta["attempts"][0]["reason"] == "fault injected (environment) after step 2"
        assert meta["status"] == "completed" and meta["resumed_from_steps"] == [2]
        assert upstream.hits == 4  # rebuilt steps came from the recording

        # Final state lives in the new container, rebuilt then continued; nothing doubled.
        assert env.exec("cat", "notes.txt").splitlines() == ["step 0", "step 1", "step 2"]
        assert env.exec("cat", "app_data/counter") == "3"
        assert env.exec("cat", "done.txt") == "counted"

        # Inside the container, `date` read the pinned step time, in the original run and on rebuild.
        events = Recording(shared_dir / "episode" / "model.jsonl").events
        for prev, ev in zip(events, events[1:]):
            clock = [r["content"] for r in ev.request["body"]["messages"][-1]["content"] if r["tool_use_id"].endswith("_1")]
            assert clock == [f"{int(prev.meta['t'])}\n"]
        assert len(Recording(shared_dir / "episode" / "mcp-fake.jsonl").events) == 3
    finally:
        env.stop()


PG_IMAGE = "resumable-evals-test-pg:latest"


@pytest.fixture(scope="session")
def pg_image():
    subprocess.run(["docker", "build", "-q", "-t", PG_IMAGE, "-f", str(HERE / "docker" / "postgres.Dockerfile"), str(HERE)],
                   check=True, capture_output=True)
    return PG_IMAGE


def test_postgres_rebuilt_by_reexecution(pg_image, shared_dir, upstream):
    """The design's core case: a database in the environment, lost with the container, rebuilt by re-execution."""
    env = DockerEnvironment(pg_image, f"resumable-test-pg-{shared_dir.name}")
    ep = Episode(
        dir=shared_dir / "episode",
        harness=["python", "/opt/eval/fake_harness.py", "{mcp_config}"],
        environment=env,
        mcp_servers={"fake": ["python", "/opt/eval/pg_mcp_server.py"]},
        upstream=upstream.url,
        faketime_lib="/usr/local/lib/libfaketime.so.1",
        timeout=180,
        fault_after_steps=2,
        fault="environment",
    )
    try:
        meta = ep.run()
        assert [a["outcome"] for a in meta["attempts"]] == ["failed", "completed"], meta
        assert meta["resumed_from_steps"] == [2] and upstream.hits == 4

        # Ids and created_at came back identical on the fresh database (the MCP proxy compared them),
        # and each created_at is exactly the pinned time of the step that inserted it.
        tool_events = Recording(shared_dir / "episode" / "mcp-fake.jsonl").events
        step_times = [int(e.meta["t"]) for e in Recording(shared_dir / "episode" / "model.jsonl").events]
        rows = [e.response["result"]["content"][0]["text"] for e in tool_events]
        assert [r.split(" ", 1)[0] for r in rows] == ["1", "2", "3"]
        final = env.exec("psql", "-h", "127.0.0.1", "-U", "postgres", "-At", "-c",
                         "SELECT id, extract(epoch FROM created_at)::bigint FROM events ORDER BY id").split()
        assert final == [f"{i + 1}|{t}" for i, t in enumerate(step_times[:3])]
    finally:
        env.stop()
