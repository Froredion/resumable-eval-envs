"""Environments: where an episode's harness, tools and MCP servers run.

An environment is started fresh from its base for every attempt; the rebuild
relies on that. Its :attr:`~Environment.clock` exists before it starts, so
the first pin is in place when its own services boot; ``start`` takes the
variables those services need (pinned clock, seeds) and returns a
:class:`Runtime`: how to run commands inside the environment and the paths
and host names things have as seen from inside it.

- :class:`DockerEnvironment` (the real one): a new container from the base
  image per attempt. The harness, the MCP proxies and the MCP servers run
  inside it via ``docker exec``. The episode directory (recordings, MCP
  config) is bind-mounted, so recordings outlive the container. The pinned
  clock file lives in the container itself, and the container is created
  with the clock variables, so the image's services (Postgres) run on it.
  The model proxy stays on the host, reached at ``host.docker.internal``.
- :class:`DirEnvironment`: a fresh copy of a directory, for local work and
  tests without Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .clock import Clock

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # directory containing resumable_evals/
CONTAINER_CLOCK = "/run/resumable-evals/clock"
CONTAINER_RANDOM = "/run/resumable-evals/random"

Command = tuple[list[str], dict[str, str], str | None]  # argv, host process env, host cwd


@dataclass
class Runtime:
    workdir: str  # harness working directory, as seen inside
    episode: str  # episode directory, as seen inside
    model_host: str  # host name of the model proxy, as seen inside
    python: list[str]  # runs Python inside, with python_env making resumable_evals importable
    python_env: dict[str, str]
    command: Callable[[list[str], dict[str, str]], Command]  # run argv inside with these env vars


class Environment(Protocol):
    proxy_bind: str  # address the model proxy listens on so the environment can reach it
    platform: str  # OS inside: "linux", "darwin"
    clock: Clock  # the environment's pinned clock; pins before start take effect at boot
    random_dir: str  # where the randomness shim keeps its counters, as seen inside; empty at every start

    def start(self, episode_dir: Path, boot_env: dict[str, str]) -> Runtime:
        """Tear down any previous instance and bring up a fresh one from the base.

        ``boot_env`` is set for everything in the environment, its own
        services included.
        """
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
    platform: str = sys.platform
    clock: Clock = field(init=False)
    random_dir: str = field(init=False)

    def __post_init__(self) -> None:
        self.clock = Clock(self.workdir.with_name(self.workdir.name + ".clock"))
        self.random_dir = str(self.workdir.with_name(self.workdir.name + ".random"))

    def start(self, episode_dir: Path, boot_env: dict[str, str]) -> Runtime:
        for d in (self.workdir, Path(self.random_dir)):
            if d.exists():
                shutil.rmtree(d)
        shutil.copytree(self.base, self.workdir, symlinks=True)
        Path(self.random_dir).mkdir(mode=0o777)
        workdir = str(self.workdir)
        return Runtime(
            workdir=workdir,
            episode=str(episode_dir),
            model_host="127.0.0.1",
            python=[sys.executable],
            python_env={},
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

    Nothing may carry state from one attempt to the next, or the rebuild
    would start from a dirty environment: containers are removed with their
    anonymous volumes (the Postgres image keeps its data in one), volumes
    listed in ``volumes`` are recreated empty per attempt, and ``run_args``
    may not mount a named volume or a writable host directory.

    On Docker Desktop for macOS the episode directory must be somewhere
    Docker shares (under ``$HOME``), or the container gets an empty mount.
    On Linux, ``proxy_bind`` must be an address the container can reach,
    such as the bridge gateway (172.17.0.1); not 0.0.0.0, which would offer
    the proxy, and the credentials it adds, to the whole network.
    """

    image: str
    name: str
    workdir: str = "/work"
    run_args: list[str] = field(default_factory=list)  # e.g. --network, --memory, read-only mounts
    volumes: dict[str, str] = field(default_factory=dict)  # name -> path inside; recreated empty every attempt
    proxy_bind: str = "127.0.0.1"
    episode_mount: str = "/resumable"
    python: str = "python"
    platform: str = "linux"
    clock: Clock = field(init=False)
    random_dir: str = CONTAINER_RANDOM

    def __post_init__(self) -> None:
        check_mounts(self.run_args)
        self.clock = Clock(CONTAINER_CLOCK, self._write_clock)
        self._staged_clock: str | None = None
        self._running = False

    def start(self, episode_dir: Path, boot_env: dict[str, str]) -> Runtime:
        self.stop()
        env_flags = [x for k, v in boot_env.items() for x in ("-e", f"{k}={v}")]
        _docker("create", "--name", self.name,
                "--add-host", "host.docker.internal:host-gateway",
                "-v", f"{episode_dir.resolve()}:{self.episode_mount}",
                "-v", f"{PACKAGE_ROOT}:/opt/resumable-evals:ro",
                *(x for key, path in self.volumes.items() for x in ("-v", f"{self._volume(key)}:{path}")),
                *env_flags, *self.run_args, self.image)
        # In place before the image's command starts its services: the clock,
        # and the randomness shim's counter directory, writable by every user
        # (Postgres runs as its own).
        staging = Path(tempfile.mkdtemp())
        try:
            run_dir = staging / "resumable-evals"
            run_dir.mkdir()
            if self._staged_clock is not None:
                (run_dir / "clock").write_text(self._staged_clock)
            (run_dir / os.path.basename(CONTAINER_RANDOM)).mkdir()
            os.chmod(run_dir / os.path.basename(CONTAINER_RANDOM), 0o777)
            _docker("cp", str(run_dir), f"{self.name}:{os.path.dirname(os.path.dirname(CONTAINER_CLOCK))}/")
        finally:
            shutil.rmtree(staging)
        _docker("start", self.name)
        self._running = True
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
            command=command,
        )

    def _write_clock(self, text: str) -> None:
        # Written inside the container (atomic rename on its own filesystem);
        # see clock.py for why not through the bind mount. Before start, staged.
        self._staged_clock = text
        if not self._running:
            return
        d = os.path.dirname(CONTAINER_CLOCK)
        try:
            _docker("exec", "-i", self.name, "sh", "-c",
                    f"mkdir -p {d} && cat > {CONTAINER_CLOCK}.tmp && mv {CONTAINER_CLOCK}.tmp {CONTAINER_CLOCK}", input=text)
        except RuntimeError:
            if self.alive():
                raise  # a running container whose clock cannot be pinned would drift unnoticed until a divergence

    def _volume(self, key: str) -> str:
        return f"{self.name}-{key}"

    def stop(self) -> None:
        _docker("rm", "-f", "-v", self.name, check=False)  # -v: its anonymous volumes too
        for key in self.volumes:
            _docker("volume", "rm", "-f", self._volume(key), check=False)
        self._running = False

    def kill(self) -> None:
        _docker("kill", self.name, check=False)

    def alive(self) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", self.name, check=False).strip() == "true"

    def exec(self, *argv: str) -> str:
        """Run a command in the container (for grading or inspecting the final state)."""
        return _docker("exec", "-w", self.workdir, self.name, *argv)


def check_mounts(run_args: list[str]) -> None:
    """Refuse ``docker run`` mounts that would keep state across attempts.

    A named volume or a writable host directory outlives the container, so a
    rebuild would start with the previous attempt's database or files. Use
    ``volumes`` for named volumes (recreated per attempt) and mount host
    directories read-only.
    """
    specs: list[tuple[str, str]] = []
    it = iter(run_args)
    for a in it:
        if a in ("-v", "--volume", "--mount"):
            specs.append((a, next(it, "")))
        elif a.startswith(("--volume=", "--mount=")):
            flag, _, value = a.partition("=")
            specs.append((flag, value))
    for flag, spec in specs:
        if flag == "--mount":
            opts = dict(kv.partition("=")[::2] for kv in spec.split(","))
            kind = opts.get("type", "volume")
            source = opts.get("source") or opts.get("src")
            readonly = "readonly" in opts or "ro" in opts
            if kind == "volume" and source:
                raise ValueError(f"--mount {spec}: named volume outlives the container; use DockerEnvironment.volumes")
            if kind == "bind" and not readonly:
                raise ValueError(f"--mount {spec}: writable host directory outlives the container; add readonly")
        else:
            parts = spec.split(":")
            if len(parts) == 1:
                continue  # anonymous volume: removed with the container
            source, opts = parts[0], parts[2].split(",") if len(parts) > 2 else []
            if source.startswith(("/", ".", "~")):
                if "ro" not in opts:
                    raise ValueError(f"-v {spec}: writable host directory outlives the container; mount it :ro")
            else:
                raise ValueError(f"-v {spec}: named volume outlives the container; use DockerEnvironment.volumes")


def _docker(*args: str, check: bool = True, input: str | None = None) -> str:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {p.stderr.strip()}")
    return p.stdout
