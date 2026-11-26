"""Orchestrator: run an episode, and rebuild and resume it when it fails.

Each attempt starts a fresh environment (see ``environments``), a model
proxy on the episode's recording, and the unmodified harness inside the
environment. The harness is pointed at the model proxy with
``ANTHROPIC_BASE_URL`` and at its MCP servers through a generated MCP config
(``{mcp_config}`` in the harness command) that runs a relay to each server's
MCP proxy. Both proxies run here, on the host, and so do the recordings:
nothing of the episode directory enters the environment, so the agent can
neither read an earlier attempt's transcript nor change what a rebuild is
checked against. Determinism controls (pinned clock,
seeds, and pinned randomness with ``random_lib``) apply to every attempt,
the original included. The seed is kept in ``episode.json``, so a resume
cannot change it.

An attempt ends in one of these ways:

- **completed**: the harness exited 0. Done.
- **failed**: the harness or the environment crashed, the harness exited
  non-zero, or the attempt ran past ``attempt_timeout`` (a hang). The next
  attempt rebuilds: same recordings, fresh environment; the proxies replay
  and check up to where the failure happened, then go live.
- **auth_failed**: the model API rejected the credential. The episode stops;
  rebuilding cannot fix that.
- **timed_out**: the episode used up ``timeout``, its live working time
  (see ``model_proxy``: replay is not charged, and a resumed attempt carries
  on from the time the episode had already used). The episode ends there,
  as an uninterrupted one would.
- **diverged**: a proxy found a re-executed result that does not match the
  recording. The harness is stopped. With ``rerun_on_divergence`` the
  recordings are moved to ``diverged-<attempt>/`` and the episode reruns
  from scratch; otherwise the episode ends as diverged.

After a completed attempt the environment is left up, so the final state
can be graded; the caller tears it down.

``episode.json`` in the episode directory records every attempt, the step
each one resumed from, and whether the final result is a resumed one, so
resumed episodes can be filtered or compared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .environments import DirEnvironment, DockerEnvironment, Environment, Runtime
from .mcp_proxy import McpListener
from .model_proxy import ModelProxy
from .recording import Recording


# Lists /proc inside the environment: every process but itself and the MCP
# relays (ours, and their port differs every attempt), by command line.
# Zombies and kernel threads have none. Ports in rewritten titles (a Postgres
# backend's "127.0.0.1(54321)") differ between runs and are masked.
PROCESS_LIST = r"""
import json, os, re
out = []
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        continue
    cmd = cmd.replace(b"\0", b" ").decode(errors="replace").strip()
    if cmd and "mcp-relay.py" not in cmd:
        out.append(re.sub(r"\(\d+\)", "(N)", cmd))
print(json.dumps(sorted(out)))
"""


@dataclass
class Episode:
    dir: Path
    harness: list[str]
    environment: Environment
    mcp_servers: dict[str, list[str]] = field(default_factory=dict)  # name -> argv inside the environment
    state_probes: dict[str, list[str]] = field(default_factory=dict)  # name -> argv inside; output checked every step
    upstream: str = "https://api.anthropic.com"
    upstream_headers: dict[str, str] = field(default_factory=dict)  # auth the proxy adds; never enters the environment
    env: dict[str, str] = field(default_factory=dict)
    faketime_lib: str | None = None
    random_lib: str | None = None  # randomness shim (librandpin.so) to preload; see shim/randpin.c
    # argv inside; the harness starts only once it exits 0, so the environment's own boot
    # (Postgres) finishes under the start pin in every attempt instead of racing the first steps
    ready: list[str] | None = None
    ready_timeout: float = 120
    # Docker only: hold each replayed reply until background jobs that had finished by then in the
    # original run have finished again, waiting at most this long (see model_proxy.py); None: off
    settle_timeout: float | None = None
    # argv inside; exits 0 when no background work (queued jobs) is pending: recorded with each
    # reply, and a rebuild waits for it where the original run was idle (see model_proxy.py)
    idle: list[str] | None = None
    clock_mode: str = "frozen"  # or "advancing"; see clock.py
    seed: int = 0
    max_attempts: int = 4
    timeout: float | None = None  # the episode's live working time, across attempts; see the module docstring
    attempt_timeout: float | None = None  # wall time per attempt, replay included: a hang; the attempt is retried
    rerun_on_divergence: bool = True
    fault_after_steps: int | None = None  # testing only: fail the first attempt once this many model steps are recorded
    fault: str = "harness"  # what fails: "harness" (killed) or "environment" (container killed)

    @property
    def meta_path(self) -> Path:
        return self.dir / "episode.json"

    def run(self) -> dict[str, Any]:
        if self.settle_timeout is not None and not isinstance(self.environment, DockerEnvironment):
            raise ValueError("settling lists the environment's processes; only a container has its own")
        if self.random_lib and self.environment.platform == "darwin":
            raise ValueError("the randomness shim is an ELF preload library; it cannot run on macOS")
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = self._load()
        if meta.setdefault("seed", self.seed) != self.seed:
            # A rebuild with another seed would draw other random values and diverge for no reason.
            raise ValueError(f"episode was recorded with seed {meta['seed']}, not {self.seed}")
        self._save(meta)  # seed and start time are settled before anything is recorded
        for _ in range(self.max_attempts - len(meta["attempts"])):
            attempt = self._attempt(len(meta["attempts"]), meta["started"])
            meta["attempts"].append(attempt)
            if attempt["outcome"] == "completed":
                meta["status"] = "completed"
            elif attempt["outcome"] in ("auth_failed", "timed_out"):
                meta["status"] = attempt["outcome"]
            elif attempt["outcome"] == "diverged":
                if self.rerun_on_divergence:
                    self._archive(attempt["attempt"])
                    meta["started"] = time.time()  # a rerun from scratch is a new original run
                else:
                    meta["status"] = "diverged"
            self._tag(meta)
            self._save(meta)
            if meta["status"] != "running":
                break
        else:
            if meta["status"] == "running":
                meta["status"] = "failed"
                self._save(meta)
        return meta

    # -- one attempt ------------------------------------------------------

    def _attempt(self, n: int, started: float) -> dict[str, Any]:
        recording = Recording(self.dir / "model.jsonl")
        recorded = len(recording.of_kind("model"))
        clock = self.environment.clock
        clock.mode = self.clock_mode
        clock.pin(started)  # boot and harness start-up see the same time in every attempt
        boot_env = clock.env([self.faketime_lib, self.random_lib], self.environment.platform) | {
            "PYTHONHASHSEED": str(self.seed),
            "RESUMABLE_EVALS_SEED": str(self.seed),
        }
        if self.random_lib:
            boot_env["RESUMABLE_EVALS_RANDOM_DIR"] = self.environment.random_dir
        rt = self.environment.start(boot_env)
        if self.ready and not self._wait_ready(rt, boot_env):
            self.environment.stop()
            return {"attempt": n, "started": time.time(), "resumed_from_step": recorded or None, "ended": time.time(),
                    "exit_code": None, "model_steps": recorded, "outcome": "failed",
                    "reason": f"environment not ready after {self.ready_timeout}s"}
        if self.random_lib:
            self._end_boot(rt)
        proxy = ModelProxy(recording, self.upstream, clock, self.upstream_headers)
        if self.timeout and proxy.live_base is None:
            self.environment.stop()
            raise ValueError("the recording has no live working time (recorded before it was kept); "
                             "resume it without --timeout, or rerun the episode")
        if self.state_probes:
            proxy.state_probe = lambda: self._probe(rt, boot_env)
        if self.settle_timeout is not None:
            proxy.processes = lambda: self._processes(rt)
            proxy.settle_timeout = self.settle_timeout
        if self.idle:
            proxy.idle = lambda: self._idle(rt, boot_env)
        httpd = proxy.serve(self.environment.proxy_bind)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        listeners = self._start_mcp(rt, boot_env | self.env)
        subst = {"workdir": rt.workdir, "mcp_config": self._put_mcp_config(rt, listeners)}
        env = boot_env | {"ANTHROPIC_BASE_URL": f"http://{rt.model_host}:{httpd.server_address[1]}"} | self.env
        argv, host_env, cwd = rt.command([a.format(**subst) for a in self.harness], env)
        record = {"attempt": n, "started": time.time(), "resumed_from_step": recorded or None}
        proc = subprocess.Popen(argv, cwd=cwd, env=host_env, stdin=subprocess.DEVNULL, start_new_session=True)
        proxy.start_live_clock()

        reason: str | None = None
        if self.fault_after_steps is not None and n == 0:
            def fault(steps: int) -> None:
                nonlocal reason
                if steps >= self.fault_after_steps and reason is None:
                    reason = f"fault injected ({self.fault}) after step {steps}"
                    if self.fault == "environment":
                        self.environment.kill()
                    _kill(proc)
            proxy.on_record = fault

        deadline = time.monotonic() + self.attempt_timeout if self.attempt_timeout else None
        timed_out = False
        while proc.poll() is None:
            if self._divergence_reports():
                reason = reason or "diverged"
                _kill(proc)
            elif self.timeout and proxy.live_time() > self.timeout:
                timed_out = reason is None
                reason = reason or f"episode used its {self.timeout}s of working time"
                _kill(proc)
            elif deadline and time.monotonic() > deadline:
                reason = reason or f"attempt timeout after {self.attempt_timeout}s"
                _kill(proc)
            time.sleep(0.1)
        _kill(proc)  # stray children: MCP relays and servers
        for listener in listeners.values():
            listener.stop()  # recordings final before anything reads them
        httpd.shutdown()
        httpd.server_close()

        reports = self._divergence_reports()
        record |= {"ended": time.time(), "exit_code": proc.returncode, "model_steps": proxy.steps,
                   "live_time": round(proxy.live_time(), 3)}
        if proxy.settle_timeouts:
            record["settle_timeouts"] = proxy.settle_timeouts
        if proxy.forwarded:
            record["model_api_passed_through"] = sorted(proxy.forwarded)
        if proxy.refused:
            record["model_api_refused"] = sorted(proxy.refused)
        refused = {name: l.refused for name, l in listeners.items() if l.refused}
        if refused:
            record["mcp_connections_refused"] = refused
        if self.random_lib:
            fallbacks = self._random_fallbacks(rt)
            if fallbacks:
                record["random_fallbacks"] = fallbacks
        if reports:
            record |= {"outcome": "diverged", "reason": "rebuild diverged", "divergence": reports}
        elif reason is None and proc.returncode == 0:
            record |= {"outcome": "completed", "reason": None}
        elif timed_out:
            record |= {"outcome": "timed_out", "reason": reason}
        elif proxy.auth_failed and reason is None:
            # Rebuilding cannot fix a credential; stop rather than retry.
            record |= {"outcome": "auth_failed", "reason": f"model API rejected the credential ({proxy.auth_failed})"}
        else:
            if reason is None:
                reason = "environment died" if not self.environment.alive() else f"harness exited {proc.returncode}"
            record |= {"outcome": "failed", "reason": reason}
        if record["outcome"] != "completed":
            self.environment.stop()
        return record

    def _probe(self, rt: Runtime, env: dict[str, str]) -> dict[str, Any]:
        """Run every state probe inside the environment. Small outputs are kept
        for divergence reports; the comparison uses exit status and hash."""
        out: dict[str, Any] = {}
        for name, argv in self.state_probes.items():
            cmd, host_env, cwd = rt.command([a.format(workdir=rt.workdir) for a in argv], env)
            try:
                p = subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
                result = {"exit": p.returncode, "sha256": hashlib.sha256(p.stdout).hexdigest()}
                text = p.stdout.decode(errors="replace")
                if len(text) <= 4096:
                    result["output"] = text
            except subprocess.TimeoutExpired:
                result = {"exit": None, "sha256": None, "output": "probe timed out"}
            out[name] = result
        return out

    def _wait_ready(self, rt: Runtime, env: dict[str, str]) -> bool:
        cmd, host_env, cwd = rt.command(self.ready or [], env)
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            p = subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True)
            if p.returncode == 0:
                return True
            time.sleep(0.2)
        return False

    def _idle(self, rt: Runtime, env: dict[str, str]) -> bool:
        cmd, host_env, cwd = rt.command(self.idle or [], env)
        try:
            return subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _processes(self, rt: Runtime) -> list[str]:
        """Command lines of the processes running in the environment, sorted."""
        cmd, host_env, cwd = rt.command([*rt.python, "-c", PROCESS_LIST], {})
        p = subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return json.loads(p.stdout) if p.returncode == 0 else ["<process list failed>"]

    def _end_boot(self, rt: Runtime) -> None:
        """Tell the randomness shim the harness is starting: draws from here on
        count apart from the boot's, whose number varies (see shim/randpin.c)."""
        cmd, host_env, cwd = rt.command(["touch", f"{self.environment.random_dir}/harness-started"], {})
        subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, check=True)

    def _random_fallbacks(self, rt: Runtime) -> list[str]:
        """Processes the randomness shim could not pin (they drew real randomness).

        Read from inside the environment, so lost with it if it died.
        """
        cmd, host_env, cwd = rt.command(["cat", f"{self.environment.random_dir}/errors"], {})
        p = subprocess.run(cmd, env=host_env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return p.stdout.splitlines() if p.returncode == 0 else []

    def _start_mcp(self, rt: Runtime, env: dict[str, str]) -> dict[str, McpListener]:
        """An MCP proxy per server, here on the host, waiting for the harness's relay."""
        return {
            name: McpListener(self.dir / f"mcp-{name}.jsonl", rt.command([a.format(workdir=rt.workdir) for a in argv], env),
                              self.environment.proxy_bind)
            for name, argv in self.mcp_servers.items()
        }

    def _put_mcp_config(self, rt: Runtime, listeners: dict[str, McpListener]) -> str:
        """Put the relay and the harness's MCP config in the environment; return the config's path inside."""
        relay = rt.put("mcp-relay.py", (Path(__file__).parent / "relay.py").read_text())
        servers = {
            name: {"command": rt.python[0], "args": [*rt.python[1:], relay, rt.model_host, str(listener.port)]}
            for name, listener in listeners.items()
        }
        return rt.put("mcp.json", json.dumps({"mcpServers": servers}, indent=2))

    def _divergence_reports(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("*.diverged.json")):
            try:
                out.append({"recording": p.name.removesuffix(".diverged.json")} | json.loads(p.read_text()))
            except ValueError:
                out.append({"recording": p.name.removesuffix(".diverged.json"), "detail": "report unreadable"})
        return out

    def _archive(self, n: int) -> None:
        dest = self.dir / f"diverged-{n}"
        dest.mkdir()
        for p in [*self.dir.glob("*.jsonl"), *self.dir.glob("*.diverged.json")]:
            p.rename(dest / p.name)

    # -- episode.json -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.meta_path.exists():
            meta = json.loads(self.meta_path.read_text())
            meta["status"] = "running"
            return meta
        return {"status": "running", "started": time.time(), "attempts": []}

    def _tag(self, meta: dict[str, Any]) -> None:
        # The run that counts is everything after the last divergence (which restarted it from scratch).
        attempts = meta["attempts"]
        last_divergence = max((i for i, a in enumerate(attempts) if a["outcome"] == "diverged"), default=-1)
        current = attempts[last_divergence + 1:]
        meta["resumed"] = any(a["resumed_from_step"] is not None for a in current)
        meta["resumed_from_steps"] = [a["resumed_from_step"] for a in current if a["resumed_from_step"] is not None]
        meta["reran_after_divergence"] = any(a["outcome"] == "diverged" for a in attempts)

    def _save(self, meta: dict[str, Any]) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        with tmp.open("w") as f:
            f.write(json.dumps(meta, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.meta_path)
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(fd)  # the rename itself survives a crash
        finally:
            os.close(fd)


def api_key_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key}


def oauth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resumable_evals.orchestrator", description=__doc__.splitlines()[0])
    parser.add_argument("--episode", required=True, type=Path, help="episode directory (recordings, episode.json)")
    parser.add_argument("--image", help="Docker image: a fresh container per attempt")
    parser.add_argument("--name", help="container name (default: from the episode directory)")
    parser.add_argument("--docker-arg", action="append", default=[], help="extra `docker run` argument (repeat)")
    parser.add_argument("--volume", action="append", default=[], metavar="NAME=PATH",
                        help="volume mounted at PATH, recreated empty every attempt (repeat)")
    parser.add_argument("--proxy-bind", default="127.0.0.1", help="model proxy address the container can reach")
    parser.add_argument("--base", type=Path, help="without --image: base directory, copied fresh per attempt")
    parser.add_argument("--workdir", help="harness working directory (inside the container with --image)")
    parser.add_argument("--mcp", action="append", default=[], metavar="NAME=COMMAND", help="MCP server to proxy; {workdir} expands")
    parser.add_argument("--state-probe", action="append", default=[], metavar="NAME=COMMAND",
                        help="command run inside the environment every step; its output must replay (repeat)")
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--api-key-env", help="host env var holding an Anthropic API key the proxy adds upstream")
    parser.add_argument("--oauth-token-env", help="host env var holding a Claude OAuth token the proxy adds upstream")
    parser.add_argument("--faketime-lib", help="libfaketime library to preload into the harness and everything it starts")
    parser.add_argument("--ready", metavar="COMMAND",
                        help="command run inside the environment until it succeeds, before the harness starts")
    parser.add_argument("--ready-timeout", type=float, default=120)
    parser.add_argument("--settle-timeout", type=float, default=30,
                        help="with --image: seconds a replayed reply waits for background jobs to finish as they had "
                             "in the original run (0: don't wait)")
    parser.add_argument("--idle", metavar="COMMAND",
                        help="command run inside the environment that succeeds when no background work is pending; "
                             "a rebuild waits for it wherever the original run was idle")
    parser.add_argument("--random-lib", help="randomness shim (librandpin.so) to preload into everything in the environment")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="environment variable for the harness (repeat)")
    parser.add_argument("--clock", choices=["frozen", "advancing"], default="frozen", help="pinned clock mode (see clock.py)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float,
                        help="the episode's live working time in seconds, across attempts (replay is not charged); "
                             "using it up ends the episode as timed_out")
    parser.add_argument("--attempt-timeout", type=float,
                        help="wall time per attempt in seconds, replay included; past it the attempt is a hang and is retried")
    parser.add_argument("--no-rerun-on-divergence", action="store_true")
    parser.add_argument("--fault-after-steps", type=int, help="testing: fail the first attempt after N model steps")
    parser.add_argument("--fault", choices=["harness", "environment"], default="harness")
    parser.add_argument("harness", nargs=argparse.REMAINDER, help="-- harness command; {mcp_config}, {workdir} expand")
    args = parser.parse_args(argv)
    cmd = args.harness[1:] if args.harness[:1] == ["--"] else args.harness
    if not cmd:
        parser.error("missing harness command after --")
    servers = {}
    for spec in args.mcp:
        name, _, command = spec.partition("=")
        servers[name] = shlex.split(command)
    probes = {}
    for spec in args.state_probe:
        name, _, command = spec.partition("=")
        probes[name] = ["sh", "-c", command]
    if args.image:
        name = args.name or f"resumable-{args.episode.resolve().name}"
        volumes = dict(v.partition("=")[::2] for v in args.volume)
        environment: Environment = DockerEnvironment(args.image, name, workdir=args.workdir or "/work",
                                                     run_args=args.docker_arg, volumes=volumes,
                                                     proxy_bind=args.proxy_bind)
    elif args.base and args.workdir:
        environment = DirEnvironment(args.base, Path(args.workdir))
    else:
        parser.error("give --image, or --base and --workdir")
    headers = {}
    # pop: read once, then gone from this process's environment, so nothing it starts inherits the credential
    if args.api_key_env:
        headers = api_key_headers(os.environ.pop(args.api_key_env))
    elif args.oauth_token_env:
        headers = oauth_headers(os.environ.pop(args.oauth_token_env))
    episode = Episode(
        dir=args.episode, harness=cmd, environment=environment, mcp_servers=servers, state_probes=probes,
        upstream=args.upstream,
        env=dict(kv.partition("=")[::2] for kv in args.env),
        upstream_headers=headers, faketime_lib=args.faketime_lib, random_lib=args.random_lib,
        ready=["sh", "-c", args.ready] if args.ready else None, ready_timeout=args.ready_timeout,
        settle_timeout=args.settle_timeout if args.image and args.settle_timeout > 0 else None,
        idle=["sh", "-c", args.idle] if args.idle else None, clock_mode=args.clock, seed=args.seed, max_attempts=args.max_attempts,
        timeout=args.timeout, attempt_timeout=args.attempt_timeout, rerun_on_divergence=not args.no_rerun_on_divergence,
        fault_after_steps=args.fault_after_steps, fault=args.fault,
    )
    meta = episode.run()
    print(json.dumps({k: meta[k] for k in ("status", "resumed", "resumed_from_steps", "reran_after_divergence") if k in meta}))
    return 0 if meta["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
