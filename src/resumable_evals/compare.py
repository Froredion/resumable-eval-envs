"""Compare a re-executed tool result with its recording.

Some output is volatile even in a deterministic environment (durations,
PIDs). Normalization rules replace those spans with a placeholder on both
sides before comparing. Add a rule only with a test that shows the field;
never loosen comparison to make a rebuild pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    (r"\b\d+(\.\d+)?\s?(ms|s|sec|seconds)\b", "<duration>"),
)


@dataclass(frozen=True)
class Mismatch:
    expected: str
    actual: str


def normalize(value: Any, rules: tuple[tuple[str, str], ...] = DEFAULT_RULES) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text)
    return text


def compare(expected: Any, actual: Any, rules: tuple[tuple[str, str], ...] = DEFAULT_RULES) -> Mismatch | None:
    e, a = normalize(expected, rules), normalize(actual, rules)
    return None if e == a else Mismatch(e, a)
