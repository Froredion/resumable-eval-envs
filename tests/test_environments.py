import pytest

from resumable_evals.environments import check_mounts


@pytest.mark.parametrize("args", [
    ["-v", "pgdata:/var/lib/postgresql/data"],
    ["--volume=pgdata:/var/lib/postgresql/data"],
    ["--mount", "type=volume,source=pgdata,target=/data"],
    ["--mount", "source=pgdata,target=/data"],
    ["-v", "/srv/fixtures:/fixtures"],
    ["-v", "./fixtures:/fixtures:z"],
    ["--mount", "type=bind,source=/srv,target=/srv"],
])
def test_mounts_that_keep_state_across_attempts_are_refused(args):
    with pytest.raises(ValueError, match="outlives the container"):
        check_mounts(["--network", "none", *args])


@pytest.mark.parametrize("args", [
    ["-v", "/var/lib/postgresql/data"],
    ["-v", "/srv/fixtures:/fixtures:ro"],
    ["-v", "/srv/fixtures:/fixtures:ro,z"],
    ["--mount", "type=bind,source=/srv,target=/srv,readonly"],
    ["--mount", "type=tmpfs,target=/tmp"],
    ["--network", "none", "--memory", "2g"],
])
def test_fresh_or_read_only_mounts_are_allowed(args):
    check_mounts(args)
