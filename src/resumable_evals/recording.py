"""Append-only recording store: one JSONL file per episode, fsynced per event.

Each line is one event: a model exchange or a tool call. Events carry a
monotonically increasing ``seq`` and the agent ``step`` they belong to. A
trailing partial line (the process died mid-write) is ignored on load: that
event never completed, so it is the one in-progress step the design allows
to be lost.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["model", "tool"]


@dataclass(frozen=True)
class Event:
    seq: int
    step: int
    kind: Kind
    request: Any
    response: Any
    meta: dict[str, Any] = field(default_factory=dict)


class Recording:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.events: list[Event] = list(self._load())

    def _load(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        good = 0
        with self.path.open("rb") as f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # torn final write
                yield Event(**json.loads(raw))
                good += len(raw)
        if good < self.path.stat().st_size:
            # Drop the torn tail so the next append starts on a clean line.
            with self.path.open("r+b") as f:
                f.truncate(good)
                f.flush()
                os.fsync(f.fileno())

    @property
    def last_step(self) -> int:
        return self.events[-1].step if self.events else -1

    def append(self, step: int, kind: Kind, request: Any, response: Any, **meta: Any) -> Event:
        event = Event(len(self.events), step, kind, request, response, meta)
        line = json.dumps(asdict(event), separators=(",", ":"), sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as f:
            f.write(line.encode())
            f.flush()
            os.fsync(f.fileno())
        self.events.append(event)
        return event

    def of_kind(self, kind: Kind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]
