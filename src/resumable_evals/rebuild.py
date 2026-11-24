"""Rebuild cursor: walks a recording while the harness replays.

Model requests are answered from the recording by order, not by content
(harnesses may put timestamps or ids in prompts). Requests the original run
sent concurrently (subagents) were recorded as overlapping; on rebuild they
may arrive in another order, so within such a group the caller picks the
event a request matches (:meth:`RebuildCursor.model_window`). Tool calls are re-executed
by the caller against the fresh environment; ``check_tool`` compares the
result with the recording. Once the recording is exhausted the cursor is
live and the proxies forward calls normally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    _model_i: int = 0  # first model event not yet answered
    _tool_i: int = 0
    _taken: set[int] = field(default_factory=set)  # model events answered out of order, past _model_i

    def __post_init__(self) -> None:
        self._models = self.recording.of_kind("model")
        self._tools = self.recording.of_kind("tool")

    @property
    def live(self) -> bool:
        return self._model_i >= len(self._models) and self._tool_i >= len(self._tools)

    @property
    def resumed_from_step(self) -> int:
        return self.recording.last_step + 1

    def peek_model(self) -> Event | None:
        """Recorded event the next model request will be answered from, or None once live."""
        return self._models[self._model_i] if self._model_i < len(self._models) else None

    def next_model_reply(self) -> Any | None:
        """Recorded reply for the next model request, or None once live."""
        event = self.peek_model()
        if event is None:
            return None
        return self.take_model(event)

    def model_window(self) -> list[Event]:
        """Recorded events the next model request may be answered from.

        The first unanswered event, plus the events after it that the
        original run received while an earlier request was still in flight
        (``overlaps_previous``): those may arrive in any order on rebuild.
        Empty once live.
        """
        out = []
        for i in range(self._model_i, len(self._models)):
            if i > self._model_i and not self._models[i].meta.get("overlaps_previous"):
                break
            if i not in self._taken:
                out.append(self._models[i])
        return out

    def unanswered_models(self) -> list[Event]:
        """Every recorded model event not yet answered, in order."""
        return [e for i, e in enumerate(self._models) if i >= self._model_i and i not in self._taken]

    def take_model(self, event: Event) -> Any:
        """Mark a model event from :meth:`model_window` answered; return its reply."""
        i = self._models.index(event)
        self._taken.add(i)
        while self._model_i in self._taken:
            self._taken.discard(self._model_i)
            self._model_i += 1
        return event.response

    def next_tool_call(self) -> Event | None:
        if self._tool_i >= len(self._tools):
            return None
        return self._tools[self._tool_i]

    def expect_tool(self, request: Any) -> Event | None:
        """Recorded event this request must replay, or None once live.

        Raises Diverged if the request is not the next recorded one, so the
        caller can refuse to execute it against the environment at all.
        """
        event = self.next_tool_call()
        if event is not None and request != event.request:
            raise Diverged(event, f"request differs: recorded {event.request!r}, got {request!r}")
        return event

    def check_tool(self, request: Any, result: Any) -> None:
        """Check a re-executed tool call against the recording; raise Diverged on mismatch."""
        event = self.expect_tool(request)
        if event is None:
            return  # live
        mismatch = compare(event.response, result)
        if mismatch:
            raise Diverged(event, mismatch)
        self._tool_i += 1
