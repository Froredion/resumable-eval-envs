from resumable_evals.recording import Recording


def test_append_and_reload(tmp_path):
    p = tmp_path / "ep.jsonl"
    r = Recording(p)
    r.append(0, "model", {"m": "hi"}, {"text": "call ls"})
    r.append(0, "tool", {"tool": "ls"}, "a.txt")
    again = Recording(p)
    assert [e.kind for e in again.events] == ["model", "tool"]
    assert again.last_step == 0


def test_torn_final_line_is_dropped(tmp_path):
    p = tmp_path / "ep.jsonl"
    r = Recording(p)
    r.append(0, "tool", {"tool": "ls"}, "a.txt")
    with p.open("ab") as f:
        f.write(b'{"seq":1,"step":1,"kind":"to')
    assert len(Recording(p).events) == 1
