"""Pinned clock for the environment, through libfaketime's timestamp file.

The model proxy pins the clock each time it hands the harness a model reply:
to the real time in the original run (and records it), to the recorded time
on rebuild. Everything the agent does in that step (tool calls, its own shell
commands) then sees the same time in both runs, so logs, ``date`` output
and ``created_at`` columns match. Each process starts at the pinned instant
and its clock runs forward from there rather than freezing, so sleeps and
timeouts still work; monotonic clocks are left alone. A new pin takes effect
for the next clock read, in processes already running too.

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
    def __init__(self, path: Path | str, write: Callable[[str], None] | None = None):
        self.path = Path(path)  # where the environment's processes read it
        self._write = write or self._write_local

    def pin(self, t: float) -> None:
        """Make the environment's clock read ``t`` (epoch seconds) from now on."""
        self._write(f"@{int(t)}\n")

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
