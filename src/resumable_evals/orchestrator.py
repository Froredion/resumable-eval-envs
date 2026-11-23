"""Orchestrator: run an episode, and rebuild and resume it when it fails.

Each attempt starts a fresh environment, a model proxy on the episode's
recording, and the unmodified harness. The harness is pointed at the model
proxy with ``ANTHROPIC_BASE_URL`` and at its MCP servers through a generated
MCP config (``{mcp_config}`` in the harness command) that wraps every server
in the MCP proxy. Determinism controls (pinned clock, seeds) apply to every
attempt, the original included.

An attempt ends one of three ways:

- **completed**: the harness exited 0. Done.
- **failed**: the harness crashed, exited non-zero or timed out. The next
  attempt rebuilds: same recordings, fresh environment; the proxies replay
  and check up to where the failure happened, then go live.
- **diverged**: a proxy found a re-executed result that does not match the
  recording. The harness is stopped. With ``rerun_on_divergence`` the
  recordings are moved to ``diverged-<attempt>/`` and the episode reruns
  from scratch; otherwise the episode ends as diverged.

``episode.json`` in the episode directory records every attempt, the step
each one resumed from, and whether the final result is a resumed one, so
resumed episodes can be filtered or compared.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .clock import Clock
from .model_proxy import ModelProxy
from .recording import Recording


class Environment(Protocol):
    def start(self) -> Path:
        """Bring up a fresh environment from its base; return the harness working directory."""
        ...

    def stop(self) -> None: ...


@dataclass
class DirEnvironment:
    """Local stand-in for a container: a fresh copy of ``base`` at the same path on every start.

    The path must not change between attempts: it appears in tool results
    (file paths, ``pwd``), which the rebuild compares.
    """

    base: Path
    workdir: Path

    def start(self) -> Path:
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        shutil.copytree(self.base, self.workdir, symlinks=True)
        return self.workdir

    def stop(self) -> None:
        pass  # left in place for inspection; the next start replaces it


@dataclass
class Episode:
    dir: Path
    harness: list[str]
    environment: Environment
    mcp_servers: dict[str, list[str]] = field(default_factory=dict)
    upstream: str = "https://api.anthropic.com"
    env: dict[str, str] = field(default_factory=dict)
    faketime_lib: str | None = None
    seed: int = 0
    max_attempts: int = 4
    timeout: float | None = None
    rerun_on_divergence: bool = True
    fault_after_steps: int | None = None  # testing only: kill the first attempt once this many model steps are recorded

    @property
    def meta_path(self) -> Path:
        return self.dir / "episode.json"

    def run(self) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = self._load()
        for _ in range(self.max_attempts - len(meta["attempts"])):
            attempt = self._attempt(len(meta["attempts"]), meta["started"])
            meta["attempts"].append(attempt)
            if attempt["outcome"] == "completed":
                meta["status"] = "completed"
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
        clock = Clock(self.dir / "clock")
        clock.pin(started)  # harness start-up sees the same time in every attempt

        workdir = self.environment.start()
        proxy = ModelProxy(recording, self.upstream, clock)
        httpd = proxy.serve()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        subst = {"workdir": str(workdir), "mcp_config": str(self._write_mcp_config(workdir)), "episode": str(self.dir)}
        env = os.environ | clock.env(self.faketime_lib) | {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
            "PYTHONHASHSEED": str(self.seed),
            "RESUMABLE_EVALS_SEED": str(self.seed),
        } | self.env
        record = {"attempt": n, "started": time.time(), "resumed_from_step": recorded or None}
        proc = subprocess.Popen([a.format(**subst) for a in self.harness], cwd=workdir, env=env,
                                stdin=subprocess.DEVNULL, start_new_session=True)

        reason: str | None = None
        if self.fault_after_steps is not None and n == 0:
            def fault(steps: int) -> None:
                nonlocal reason
                if steps >= self.fault_after_steps and reason is None:
                    reason = f"fault injected after step {steps}"
                    _kill(proc)
            proxy.on_record = fault

        deadline = time.monotonic() + self.timeout if self.timeout else None
        while proc.poll() is None:
            if self._divergence_reports():
                reason = reason or "diverged"
                _kill(proc)
            elif deadline and time.monotonic() > deadline:
                reason = reason or f"timeout after {self.timeout}s"
                _kill(proc)
            time.sleep(0.1)
        _kill(proc)  # stray children: MCP proxies and servers
        httpd.shutdown()
        httpd.server_close()
        self.environment.stop()

        reports = self._divergence_reports()
        record |= {"ended": time.time(), "exit_code": proc.returncode, "model_steps": proxy.steps}
        if reports:
            record |= {"outcome": "diverged", "reason": "rebuild diverged", "divergence": reports}
        elif reason is None and proc.returncode == 0:
            record |= {"outcome": "completed", "reason": None}
        else:
            record |= {"outcome": "failed", "reason": reason or f"harness exited {proc.returncode}"}
        return record

    def _write_mcp_config(self, workdir: Path) -> Path:
        servers = {
            name: {
                "command": sys.executable,
                "args": ["-m", "resumable_evals.mcp_proxy", "--recording", str(self.dir / f"mcp-{name}.jsonl"), "--",
                         *(a.format(workdir=workdir) for a in argv)],
            }
            for name, argv in self.mcp_servers.items()
        }
        path = self.dir / "mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}, indent=2))
        return path

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
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, self.meta_path)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resumable_evals.orchestrator", description=__doc__.splitlines()[0])
    parser.add_argument("--episode", required=True, type=Path, help="episode directory (recordings, episode.json)")
    parser.add_argument("--base", required=True, type=Path, help="base environment directory, copied fresh per attempt")
    parser.add_argument("--workdir", required=True, type=Path, help="where the environment lives; same path every attempt")
    parser.add_argument("--mcp", action="append", default=[], metavar="NAME=COMMAND", help="MCP server to proxy; {workdir} expands")
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--faketime-lib", help="libfaketime library to preload into the harness and everything it starts")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float, help="seconds per attempt")
    parser.add_argument("--no-rerun-on-divergence", action="store_true")
    parser.add_argument("--fault-after-steps", type=int, help="testing: kill the first attempt after N model steps")
    parser.add_argument("harness", nargs=argparse.REMAINDER, help="-- harness command; {mcp_config}, {workdir} expand")
    args = parser.parse_args(argv)
    cmd = args.harness[1:] if args.harness[:1] == ["--"] else args.harness
    if not cmd:
        parser.error("missing harness command after --")
    servers = {}
    for spec in args.mcp:
        name, _, command = spec.partition("=")
        servers[name] = shlex.split(command)
    episode = Episode(
        dir=args.episode, harness=cmd, environment=DirEnvironment(args.base, args.workdir), mcp_servers=servers,
        upstream=args.upstream, faketime_lib=args.faketime_lib, seed=args.seed, max_attempts=args.max_attempts,
        timeout=args.timeout, rerun_on_divergence=not args.no_rerun_on_divergence, fault_after_steps=args.fault_after_steps,
    )
    meta = episode.run()
    print(json.dumps({k: meta[k] for k in ("status", "resumed", "resumed_from_steps", "reran_after_divergence") if k in meta}))
    return 0 if meta["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
