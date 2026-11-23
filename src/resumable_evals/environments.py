"""Environments: where an episode's harness, tools and MCP servers run.

An environment is started fresh from its base for every attempt; the rebuild
relies on that. :meth:`Environment.start` returns a :class:`Runtime`: how to
run commands inside the environment and the paths and host names things have
as seen from inside it.

- :class:`DockerEnvironment` (the real one): a new container from the base
  image per attempt. The harness, the MCP proxies and the MCP servers run
  inside it via ``docker exec``. The episode directory (recordings, MCP
  config) is bind-mounted, so recordings outlive the container. The pinned
  clock file lives in the container itself.
  The model proxy stays on the host, reached at ``host.docker.internal``.
- :class:`DirEnvironment`: a fresh copy of a directory, for local work and
  tests without Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .clock import Clock

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # directory containing resumable_evals/
CONTAINER_CLOCK = "/run/resumable-evals/clock"

Command = tuple[list[str], dict[str, str], str | None]  # argv, host process env, host cwd


@dataclass
class Runtime:
    workdir: str  # harness working directory, as seen inside
    episode: str  # episode directory, as seen inside
    model_host: str  # host name of the model proxy, as seen inside
    python: list[str]  # runs Python inside, with python_env making resumable_evals importable
    python_env: dict[str, str]
    platform: str  # OS inside: "linux", "darwin"
    clock: Clock  # the environment's pinned clock, readable inside
    command: Callable[[list[str], dict[str, str]], Command]  # run argv inside with these env vars


class Environment(Protocol):
    proxy_bind: str  # address the model proxy listens on so the environment can reach it

    def start(self, episode_dir: Path) -> Runtime:
        """Tear down any previous instance and bring up a fresh one from the base."""
        ...

    def stop(self) -> None:
        """Tear down. Not called after a completed attempt: the final state stays up for grading."""
        ...

    def kill(self) -> None:
        """Make the environment fail abruptly (fault injection)."""
        ...

    def alive(self) -> bool: ...


@dataclass
class DirEnvironment:
    """A fresh copy of ``base`` at the same path on every start.

    The path must not change between attempts: it appears in tool results
    (file paths, ``pwd``), which the rebuild compares.
    """

    base: Path
    workdir: Path
    proxy_bind: str = "127.0.0.1"

    def start(self, episode_dir: Path) -> Runtime:
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        shutil.copytree(self.base, self.workdir, symlinks=True)
        workdir = str(self.workdir)
        return Runtime(
            workdir=workdir,
            episode=str(episode_dir),
            model_host="127.0.0.1",
            python=[sys.executable],
            python_env={},
            platform=sys.platform,
            clock=Clock(episode_dir / "clock"),
            command=lambda argv, env: (argv, dict(os.environ) | env, workdir),
        )

    def stop(self) -> None:
        pass  # the next start replaces it

    def kill(self) -> None:
        pass  # nothing to kill: a directory cannot crash; the orchestrator kills the harness

    def alive(self) -> bool:
        return self.workdir.exists()


@dataclass
class DockerEnvironment:
    """A new container from ``image`` per attempt.

    The image's own command must keep the container up (start its services,
    then wait); the harness is run with ``docker exec``. Python 3.9+ must be
    on the image's PATH as ``python``, for the MCP proxy.

    On Docker Desktop for macOS the episode directory must be somewhere
    Docker shares (under ``$HOME``), or the container gets an empty mount.
    On Linux, ``proxy_bind`` must be an address the container can reach,
    such as the bridge gateway (172.17.0.1); not 0.0.0.0, which would offer
    the proxy, and the credentials it adds, to the whole network.
    """

    image: str
    name: str
    workdir: str = "/work"
    run_args: list[str] = field(default_factory=list)  # e.g. --network, extra volumes, --memory
    proxy_bind: str = "127.0.0.1"
    episode_mount: str = "/resumable"
    python: str = "python"

    def start(self, episode_dir: Path) -> Runtime:
        self.stop()
        _docker("run", "-d", "--name", self.name,
                "--add-host", "host.docker.internal:host-gateway",
                "-v", f"{episode_dir.resolve()}:{self.episode_mount}",
                "-v", f"{PACKAGE_ROOT}:/opt/resumable-evals:ro",
                *self.run_args, self.image)
        deadline = time.monotonic() + 30
        while not self.alive():
            if time.monotonic() > deadline:
                raise RuntimeError(f"container {self.name} did not start: {_docker('logs', self.name, check=False)}")
            time.sleep(0.2)

        def command(argv: list[str], env: dict[str, str]) -> Command:
            flags = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
            return ["docker", "exec", "-i", "-w", self.workdir, *flags, self.name, *argv], dict(os.environ), None

        return Runtime(
            workdir=self.workdir,
            episode=self.episode_mount,
            model_host="host.docker.internal",
            python=[self.python],
            python_env={"PYTHONPATH": "/opt/resumable-evals"},
            platform="linux",
            clock=Clock(CONTAINER_CLOCK, self._write_clock),
            command=command,
        )

    def _write_clock(self, text: str) -> None:
        # Written inside the container (atomic rename on its own filesystem);
        # see clock.py for why not through the bind mount.
        d = os.path.dirname(CONTAINER_CLOCK)
        _docker("exec", "-i", self.name, "sh", "-c", f"mkdir -p {d} && cat > {CONTAINER_CLOCK}.tmp && mv {CONTAINER_CLOCK}.tmp {CONTAINER_CLOCK}",
                input=text)

    def stop(self) -> None:
        _docker("rm", "-f", self.name, check=False)

    def kill(self) -> None:
        _docker("kill", self.name, check=False)

    def alive(self) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", self.name, check=False).strip() == "true"

    def exec(self, *argv: str) -> str:
        """Run a command in the container (for grading or inspecting the final state)."""
        return _docker("exec", "-w", self.workdir, self.name, *argv)


def _docker(*args: str, check: bool = True, input: str | None = None) -> str:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {p.stderr.strip()}")
    return p.stdout
