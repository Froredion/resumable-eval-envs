"""Pinned clock for the environment, through libfaketime's timestamp file.

The model proxy pins the clock each time it hands the harness a model reply:
to the real time in the original run (and records it), to the recorded time
on rebuild. Everything the agent does in that step (tool calls, its own shell
commands, services already running) then sees the same time in both runs, so
logs, ``date`` output and ``created_at`` columns match.

Two modes:

- ``frozen`` (default): wall-clock time reads exactly the pinned instant for
  the whole step, to the nanosecond, in every process. Monotonic clocks are
  not faked, so sleeps, ``timeout``, timers and most timeouts still work;
  code that measures elapsed time with the wall clock sees zero.
- ``advancing``: wall-clock time starts at the pinned instant and runs on.
  Anything read partway through a step then depends on how long the step
  took, so sub-second timestamps (Postgres ``now()``) differ between runs.

A new pin takes effect at the next clock read, in processes already running
too.

Processes see the pinned clock when started with :meth:`Clock.env` and a
libfaketime library to preload (``libfaketime.so.1`` in a Linux image).
Checked with libfaketime 0.9.10 (Debian) and 0.9.14 (built from source,
which Claude Code needs; see examples/claude-code/Dockerfile).

The file must live where the environment's processes read it reliably. In a
container that means the container's own filesystem, written with
``docker exec`` (see ``environments``): through Docker Desktop's bind mounts
a file updated on the host reads as missing for a while after a rename (and
libfaketime silently falls back to the real clock), or keeps its old
content after an in-place write.

Not covered: file modification times. The kernel stamps them, so ``ls -l``
or ``stat`` output differs between runs; the per-step result check reports
that as a divergence until a normalization rule (with a test) covers it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable


class Clock:
    def __init__(self, path: Path | str, write: Callable[[str], None] | None = None, mode: str = "frozen"):
        self.path = Path(path)  # where the environment's processes read it
        self._write = write or self._write_local
        self.mode = mode

    def pin(self, t: float) -> None:
        """Make the environment's clock read ``t`` (epoch seconds) from now on."""
        if self.mode not in ("frozen", "advancing"):
            raise ValueError(f"unknown clock mode {self.mode!r}")
        self._write(f"{'@' if self.mode == 'advancing' else ''}{int(t)}\n")

    def _write_local(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.path)  # readers never see a half-written file

    def env(self, lib: str | None = None, platform: str = sys.platform) -> dict[str, str]:
        """Environment variables that put a process on this clock.

        ``platform`` is the OS the process runs on, which decides how ``lib``
        is preloaded.
        """
        env = {
            "FAKETIME_TIMESTAMP_FILE": str(self.path),
            "FAKETIME_FMT": "%s",
            "FAKETIME_NO_CACHE": "1",  # re-read the file so each pin takes effect
            "FAKETIME_DONT_FAKE_MONOTONIC": "1",
            # >= 0.9.11; older versions ignore it. Without it, a program that
            # re-execs itself under the same PID (Claude Code does) fails to
            # create libfaketime's per-PID semaphore and exits.
            "FAKETIME_DISABLE_SHM": "1",
        }
        if lib:
            if platform == "darwin":
                env |= {"DYLD_INSERT_LIBRARIES": lib, "DYLD_FORCE_FLAT_NAMESPACE": "1"}
            else:
                env["LD_PRELOAD"] = lib
        return env
