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
Checked with libfaketime 0.9.10 on Debian bookworm, with the file written by
the host into a bind-mounted directory.

Not covered: file modification times. The kernel stamps them, so ``ls -l``
or ``stat`` output differs between runs; the per-step result check reports
that as a divergence until a normalization rule (with a test) covers it.
On Docker Desktop for macOS, keep the clock file under a directory Docker
shares (``$HOME``, not ``/var/folders``), or the container sees an empty
directory and the real clock.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class Clock:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def pin(self, t: float) -> None:
        """Make the environment's clock read ``t`` (epoch seconds) from now on."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(f"@{int(t)}\n")
        os.replace(tmp, self.path)  # readers never see a half-written file

    def env(self, lib: str | None = None) -> dict[str, str]:
        """Environment variables that put a process on this clock."""
        env = {
            "FAKETIME_TIMESTAMP_FILE": str(self.path),
            "FAKETIME_FMT": "%s",
            "FAKETIME_NO_CACHE": "1",  # re-read the file so each pin takes effect
            "FAKETIME_DONT_FAKE_MONOTONIC": "1",
        }
        if lib:
            if sys.platform == "darwin":
                env |= {"DYLD_INSERT_LIBRARIES": lib, "DYLD_FORCE_FLAT_NAMESPACE": "1"}
            else:
                env["LD_PRELOAD"] = lib
        return env
