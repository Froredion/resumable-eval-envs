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
SHIM_CONTEXT = ["--build-context", f"randpin={HERE.parent / 'src' / 'resumable_evals' / 'shim'}"]
RANDPIN = "/usr/local/lib/librandpin.so"
FAKETIME = "/usr/local/lib/libfaketime.so.1"


def docker_ok():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason="Docker not available")


@pytest.fixture(scope="session")
def image():
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, *SHIM_CONTEXT, "-f", str(HERE / "docker" / "Dockerfile"), str(HERE)],
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
    subprocess.run(["docker", "build", "-q", "-t", PG_IMAGE, *SHIM_CONTEXT, "-f", str(HERE / "docker" / "postgres.Dockerfile"), str(HERE)],
                   check=True, capture_output=True)
    return PG_IMAGE


PSQL = ["psql", "-h", "127.0.0.1", "-U", "postgres", "-qAt", "-c"]


def dangling_volumes():
    return set(subprocess.run(["docker", "volume", "ls", "-qf", "dangling=true"], capture_output=True, text=True).stdout.split())


def test_postgres_rebuilt_by_reexecution(pg_image, shared_dir, upstream):
    """The design's core case: a database in the environment, lost with the container, rebuilt by re-execution.

    The data directory is a named volume, recreated empty for the rebuild; the
    events table is checked against the recording at every step.
    """
    before = dangling_volumes()
    env = DockerEnvironment(pg_image, f"resumable-test-pg-{shared_dir.name}",
                            volumes={"pgdata": "/var/lib/postgresql/data"})
    ep = Episode(
        state_probes={"events": [*PSQL, "SELECT id, created_at FROM events ORDER BY id"]},
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
        recorded_states = [e.meta["state"]["events"] for e in Recording(shared_dir / "episode" / "model.jsonl").events]
        assert recorded_states[-1]["output"].count("\n") == 3  # the probe saw all three rows by the last step
    finally:
        env.stop()
    leftover = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={env.name}"], capture_output=True, text=True)
    assert leftover.stdout.strip() == ""
    assert dangling_volumes() <= before  # anonymous volumes went with their containers


def hidden_effect_episode(pg_image, shared_dir, upstream, probes):
    env = DockerEnvironment(pg_image, f"resumable-test-hid-{shared_dir.name}")
    ep = Episode(
        dir=shared_dir / "episode",
        harness=["python", "/opt/eval/fake_harness.py", "{mcp_config}"],
        environment=env,
        mcp_servers={"fake": ["env", "PG_HIDDEN_RANDOM=1", "python", "/opt/eval/pg_mcp_server.py"]},
        state_probes=probes,
        upstream=upstream.url,
        faketime_lib="/usr/local/lib/libfaketime.so.1",
        timeout=180,
        fault_after_steps=2,
        fault="environment",
        rerun_on_divergence=False,
    )
    return env, ep


def test_hidden_database_difference_is_caught_by_a_state_probe(pg_image, shared_dir, upstream):
    env, ep = hidden_effect_episode(pg_image, shared_dir, upstream,
                                    {"side": [*PSQL, "SELECT id, v FROM side ORDER BY id"]})
    try:
        meta = ep.run()
    finally:
        env.stop()
    assert meta["status"] == "diverged"
    report = meta["attempts"][1]["divergence"][0]
    assert report["recording"] == "model.jsonl" and "environment state differs: side" in report["detail"]
    assert report["recorded_state"]["side"]["output"] != report["state"]["side"]["output"]


def test_without_a_probe_a_hidden_difference_goes_unseen(pg_image, shared_dir, upstream):
    """Why probes exist: every tool result matches, so the rebuild passes on a different database."""
    env, ep = hidden_effect_episode(pg_image, shared_dir, upstream, {})
    try:
        meta = ep.run()
    finally:
        env.stop()
    assert meta["status"] == "completed" and meta["resumed"]


# -- pinned randomness ------------------------------------------------------

DRAWS = r"""
import os, subprocess, uuid
print(uuid.uuid4(), uuid.uuid4())
print(subprocess.run(["mktemp"], capture_output=True, text=True).stdout.strip())
print(subprocess.run(["head", "-c", "8", "/dev/urandom"], capture_output=True).stdout.hex())
r, w = os.pipe()
if os.fork() == 0:
    os.write(w, os.urandom(8)); os._exit(0)
os.wait()
print(os.read(r, 8).hex(), os.urandom(8).hex())
"""


def draws_in_fresh_container(image, shared_dir, seed, t=1764000000):
    env = DockerEnvironment(image, f"resumable-test-rnd-{shared_dir.name}")
    env.clock.pin(t)
    boot_env = env.clock.env([FAKETIME, RANDPIN], env.platform) | {"RESUMABLE_EVALS_SEED": str(seed),
                                                      "RESUMABLE_EVALS_RANDOM_DIR": env.random_dir}
    try:
        rt = env.start(shared_dir, boot_env)
        argv, host_env, cwd = rt.command(["python", "-c", DRAWS], boot_env)
        out = subprocess.run(argv, env=host_env, cwd=cwd, capture_output=True, text=True, check=True).stdout
        errors = subprocess.run(["docker", "exec", env.name, "cat", f"{env.random_dir}/errors"], capture_output=True, text=True)
        assert errors.returncode != 0, errors.stdout  # nothing fell back to real randomness
        return out.splitlines()
    finally:
        env.stop()


def test_random_shim_replays_in_a_fresh_container(image, shared_dir):
    first = draws_in_fresh_container(image, shared_dir, seed=0)
    assert draws_in_fresh_container(image, shared_dir, seed=0) == first
    other = draws_in_fresh_container(image, shared_dir, seed=7)
    assert all(a != b for a, b in zip(first, other))
    uuid_a, uuid_b = first[0].split()
    assert uuid_a != uuid_b  # two draws in one step differ
    child, parent = first[3].split()
    assert child != parent  # a forked child takes its own stream


def uuid_episode(pg_image, shared_dir, upstream, random_lib):
    env = DockerEnvironment(pg_image, f"resumable-test-uuid-{shared_dir.name}", volumes={"pgdata": "/var/lib/postgresql/data"})
    ep = Episode(
        dir=shared_dir / "episode",
        harness=["python", "/opt/eval/fake_harness.py", "{mcp_config}"],
        environment=env,
        mcp_servers={"fake": ["env", "PG_UUID=1", "python", "/opt/eval/pg_mcp_server.py"]},
        upstream=upstream.url,
        faketime_lib=FAKETIME,
        random_lib=random_lib,
        ready=["pg_isready", "-h", "127.0.0.1", "-q"],  # boot finishes under the start pin, not racing step 0
        timeout=180,
        fault_after_steps=2,
        fault="environment",
        rerun_on_divergence=False,
    )
    return env, ep


def test_without_the_shim_a_database_uuid_diverges(pg_image, shared_dir, upstream):
    """Why the shim exists: gen_random_uuid() differs on the rebuilt database."""
    env, ep = uuid_episode(pg_image, shared_dir, upstream, random_lib=None)
    try:
        meta = ep.run()
    finally:
        env.stop()
    assert meta["status"] == "diverged"
    report = meta["attempts"][1]["divergence"][0]
    assert report["recording"] == "mcp-fake.jsonl" and report["seq"] == 0


def test_with_the_shim_a_database_uuid_replays(pg_image, shared_dir, upstream):
    env, ep = uuid_episode(pg_image, shared_dir, upstream, random_lib=RANDPIN)
    try:
        meta = ep.run()
    finally:
        env.stop()
    assert [a["outcome"] for a in meta["attempts"]] == ["failed", "completed"], meta
    assert meta["resumed_from_steps"] == [2] and upstream.hits == 4
    assert not any("random_fallbacks" in a for a in meta["attempts"])
    rows = [e.response["result"]["content"][0]["text"] for e in Recording(shared_dir / "episode" / "mcp-fake.jsonl").events]
    uuids = [r.split()[-1] for r in rows]
    assert len(uuids) == 3 and len(set(uuids)) == 3  # distinct, and the first two replayed exactly


# -- background jobs ----------------------------------------------------------

def background_episode(image, shared_dir, settle_timeout):
    """Step 0 starts a one-second job; the model takes two; step 1 reads its output.

    In the original run the job is done before the read. A replay answers at
    once, so without settling the read comes too early.
    """
    up = Upstream(script=[["bg"], ["read"]], delay=2)
    env = DockerEnvironment(image, f"resumable-test-bg-{shared_dir.name}")
    ep = Episode(
        dir=shared_dir / "episode",
        harness=["python", "/opt/eval/fake_harness.py", "{mcp_config}"],
        environment=env,
        mcp_servers={"fake": ["python", "/opt/eval/fake_mcp_server.py", "{workdir}/app_data"]},
        upstream=up.url,
        faketime_lib=FAKETIME,
        settle_timeout=settle_timeout,
        timeout=120,
        fault_after_steps=3,  # after the last step: everything replays
        fault="environment",
        rerun_on_divergence=False,
    )
    return env, ep, up


def test_without_settling_a_background_job_is_read_too_early(image, shared_dir):
    env, ep, up = background_episode(image, shared_dir, settle_timeout=None)
    try:
        meta = ep.run()
    finally:
        env.stop()
        up.httpd.shutdown()
    assert meta["status"] == "diverged"
    assert "missing" in meta["attempts"][1]["divergence"][0]["detail"]


def test_settling_waits_for_a_background_job_as_the_original_run_did(image, shared_dir):
    env, ep, up = background_episode(image, shared_dir, settle_timeout=30)
    try:
        meta = ep.run()
        assert env.exec("cat", "bg.txt") == "done\n"
    finally:
        env.stop()
        up.httpd.shutdown()
    assert [a["outcome"] for a in meta["attempts"]] == ["failed", "completed"], meta
    assert not any("settle_timeouts" in a for a in meta["attempts"])
    events = Recording(shared_dir / "episode" / "model.jsonl").events
    assert any("fake_harness.py" in p for p in events[1].meta["processes"])  # the list is the container's
    assert not any("sleep 1" in p for p in events[1].meta["processes"])  # the job was done by step 1's reply
