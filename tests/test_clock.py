from resumable_evals.clock import Clock


def test_pin_writes_libfaketime_time(tmp_path):
    c = Clock(tmp_path / "clock")
    c.pin(1700000000.9)
    assert c.path.read_text() == "1700000000\n"  # frozen: absolute time
    assert not (tmp_path / "clock.tmp").exists()
    Clock(c.path, mode="advancing").pin(1700000000)
    assert c.path.read_text() == "@1700000000\n"  # advancing: start-at time


def test_pin_through_a_custom_writer(tmp_path):
    written = []
    c = Clock("/run/resumable-evals/clock", written.append)
    c.pin(1700000000)
    assert written == ["1700000000\n"] and c.env()["FAKETIME_TIMESTAMP_FILE"] == "/run/resumable-evals/clock"


def test_env_points_libfaketime_at_the_file(tmp_path):
    env = Clock(tmp_path / "clock").env("/usr/lib/faketime/libfaketime.so.1")
    assert env["FAKETIME_TIMESTAMP_FILE"] == str(tmp_path / "clock")
    assert env["FAKETIME_FMT"] == "%s" and env["FAKETIME_NO_CACHE"] == "1"
    assert "/usr/lib/faketime/libfaketime.so.1" in (env.get("LD_PRELOAD"), env.get("DYLD_INSERT_LIBRARIES"))
    assert Clock(tmp_path / "clock").env("/lib.so", platform="linux")["LD_PRELOAD"] == "/lib.so"
    assert "LD_PRELOAD" not in Clock(tmp_path / "clock").env() and "DYLD_INSERT_LIBRARIES" not in Clock(tmp_path / "clock").env()
