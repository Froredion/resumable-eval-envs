"""Rebuild cursor: walks a recording while the harness replays.

Model requests are answered from the recording by order, not by content
(harnesses may put timestamps or ids in prompts). Tool calls are re-executed
by the caller against the fresh environment; ``check_tool`` compares the
result with the recording. Once the recording is exhausted the cursor is
live and the proxies forward calls normally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .compare import Mismatch, compare
from .recording import Event, Recording


class Diverged(Exception):
    def __init__(self, event: Event, mismatch: Mismatch | str):
        super().__init__(f"diverged at seq {event.seq} (step {event.step}): {mismatch}")
        self.event = event
        self.mismatch = mismatch


@dataclass
class RebuildCursor:
    recording: Recording
    _model_i: int = 0
    _tool_i: int = 0

    def __post_init__(self) -> None:
        self._models = self.recording.of_kind("model")
        self._tools = self.recording.of_kind("tool")

    @property
    def live(self) -> bool:
        return self._model_i >= len(self._models) and self._tool_i >= len(self._tools)

    @property
    def resumed_from_step(self) -> int:
        return self.recording.last_step + 1

    def next_model_reply(self) -> Any | None:
        """Recorded reply for the next model request, or None once live."""
        if self._model_i >= len(self._models):
            return None
        event = self._models[self._model_i]
        self._model_i += 1
        return event.response

    def next_tool_call(self) -> Event | None:
        if self._tool_i >= len(self._tools):
            return None
        return self._tools[self._tool_i]

    def check_tool(self, request: Any, result: Any) -> None:
        """Check a re-executed tool call against the recording; raise Diverged on mismatch."""
        event = self.next_tool_call()
        if event is None:
            return  # live
        if request != event.request:
            raise Diverged(event, f"request differs: recorded {event.request!r}, got {request!r}")
        mismatch = compare(event.response, result)
        if mismatch:
            raise Diverged(event, mismatch)
        self._tool_i += 1
