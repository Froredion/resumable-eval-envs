# Contributing

Thanks for your interest. Issues and pull requests are welcome.

## Setup

```sh
uv sync
uv run pytest
uv run ruff check .
```

The Docker tests (`tests/test_docker.py`) build two small images and skip when Docker isn't available. On a Linux host, set `RESUMABLE_EVALS_TEST_PROXY_BIND` to the Docker bridge gateway so containers can reach the proxies:

```sh
export RESUMABLE_EVALS_TEST_PROXY_BIND=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')
```

## Ground rules

Read [docs/DESIGN.md](docs/DESIGN.md) before changing behaviour; it is the source of truth for record and rebuild semantics.

- **No runtime dependencies** in the core modules (`src/resumable_evals`). Dev tools go in the `dev` dependency group.
- **The record path stays crash-safe.** Every event is appended and fsynced before the call it records returns to the caller.
- **Never relax result comparison to make a rebuild pass.** A mismatch is either a divergence to flag, or a volatile field to add to the normalization rules, with a test.
- **Determinism controls (clock, seeds) apply to the original run as well as the rebuild**, or recorded timestamps cannot match.
- Behaviour changes come with a test, and a Docker test when the behaviour only shows in a real container.
- Keep the README's status and limits honest: claim what a test or a recorded run shows, and say when a cause isn't established.

## Pull requests

Keep changes focused, describe what you verified and how, and make sure CI (lint and tests on Python 3.12 and 3.13) passes. By contributing you agree that your contributions are licensed under the Apache License 2.0.
