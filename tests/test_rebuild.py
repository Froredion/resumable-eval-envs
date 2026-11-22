import pytest

from resumable_evals.rebuild import Diverged, RebuildCursor
from resumable_evals.recording import Recording


def make(tmp_path):
    r = Recording(tmp_path / "ep.jsonl")
    r.append(0, "model", "p0", "reply0")
    r.append(0, "tool", {"tool": "pytest"}, "3 passed in 0.53s")
    r.append(1, "model", "p1", "reply1")
    return r


def test_fast_forward_then_live(tmp_path):
    c = RebuildCursor(make(tmp_path))
    assert c.next_model_reply() == "reply0"
    c.check_tool({"tool": "pytest"}, "3 passed in 0.61s")  # duration normalized
    assert c.next_model_reply() == "reply1"
    assert c.live
    assert c.next_model_reply() is None
    assert c.resumed_from_step == 2


def test_result_divergence_raises(tmp_path):
    c = RebuildCursor(make(tmp_path))
    c.next_model_reply()
    with pytest.raises(Diverged):
        c.check_tool({"tool": "pytest"}, "1 failed in 0.5s")


def test_request_divergence_raises(tmp_path):
    c = RebuildCursor(make(tmp_path))
    with pytest.raises(Diverged):
        c.check_tool({"tool": "ls"}, "3 passed in 0.53s")
