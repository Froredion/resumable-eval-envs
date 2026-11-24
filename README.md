# resumable-evals

Resume an eval episode that failed partway through, from its last completed step, without changing the harness or the agent.

Environments are offline and their tools deterministic, so nothing is snapshotted. Two proxies record every model call and tool call. After a failure, a fresh container re-executes the recorded tool calls in order and checks each result against the recording. The unmodified harness fast-forwards on recorded model replies, and at the failed step both proxies go live. Design: [docs/DESIGN.md](docs/DESIGN.md).

## Layout

| Path | What |
| --- | --- |
| `src/resumable_evals/recording.py` | Append-only, fsynced JSONL recording store (one file per proxy per episode) |
| `src/resumable_evals/compare.py` | Normalizes volatile fields and compares a re-executed result to its recording |
| `src/resumable_evals/rebuild.py` | Rebuild cursor: walks a recording, checks re-executed calls, decides when to go live |
| `src/resumable_evals/model_proxy.py` | Anthropic Messages API proxy: records replies, replays them on rebuild, checks the harness's own tool results, pins the clock per step, can supply the credential itself |
| `src/resumable_evals/mcp_proxy.py` | Stdio MCP proxy: records tool calls, re-executes and checks them on rebuild |
| `src/resumable_evals/clock.py` | Per-step clock pinning through libfaketime (frozen or advancing) |
| `src/resumable_evals/shim/randpin.c` | `librandpin.so`, the preload shim that pins random values (`getrandom`, `/dev/urandom`, shared OpenSSL) per process and step |
| `src/resumable_evals/environments.py` | `DockerEnvironment` (a fresh container from the base image per attempt) and `DirEnvironment` (a fresh directory copy, for work without Docker) |
| `src/resumable_evals/orchestrator.py` | Runs an episode; on failure rebuilds a fresh environment and resumes; tags the result in `episode.json` |
| `examples/claude-code/` | Example eval image: Claude Code, a stateful MCP server, libfaketime 0.9.14, the randomness shim |
| `tests/docker/` | Test images, including one with Postgres |
| `docs/DESIGN.md` | Design doc |

## Usage

Run Claude Code in a container, with one MCP server, resuming on failure:

```sh
docker build --build-context randpin=src/resumable_evals/shim -t eval-cc examples/claude-code
inf uv run python -m resumable_evals.orchestrator \
  --episode ~/runs/ep1 --image eval-cc --workdir /work \
  --mcp "counter=python /opt/eval/counter_mcp_server.py {workdir}/app_data" \
  --faketime-lib /usr/local/lib/libfaketime.so.1 --random-lib /usr/local/lib/librandpin.so \
  --oauth-token-env CLAUDE_CODE_OAUTH_TOKEN --env ANTHROPIC_API_KEY=placeholder \
  --volume pgdata=/var/lib/postgresql/data \
  --state-probe 'db=pg_dump -U postgres --data-only app | sha256sum' \
  --ready 'pg_isready -h 127.0.0.1' \
  --timeout 1800 -- \
  claude -p "<task>" --mcp-config {mcp_config} --strict-mcp-config --allowedTools ...
```

The harness, the MCP proxies and the MCP servers run in the container; the model proxy runs on the host and adds the credential (from the host variable named by `--api-key-env` or `--oauth-token-env`), so the agent only ever sees the placeholder. `~/runs/ep1/episode.json` gets the status, every attempt, and `resumed` / `resumed_from_steps` for filtering. After a completed episode the container stays up for grading. `--fault-after-steps N --fault environment` kills the first attempt's container after N model steps, to try resume on demand. An image that should pin random values builds `librandpin.so` from `src/resumable_evals/shim` (see the example Dockerfile) and gets `--random-lib`. `--ready` holds the harness until the image's own services are up, in every attempt. `--settle-timeout` (default 30 s, 0 to turn off) bounds how long a replayed reply waits for background jobs. Without Docker, `--base DIR --workdir DIR` copies a directory fresh per attempt instead.

## Status

Rollout steps 1–3 (prototype, determinism controls, orchestrator) work with Docker environments. There are 61 tests; 9 of them run in Docker:

- A container killed mid-episode is rebuilt from its image, and the episode finishes with nothing done twice. `date` inside the container read the pinned step time in both runs.
- A container running Postgres is killed mid-episode. On the fresh database, the re-executed inserts return the same ids and `created_at`, each exactly its step's pinned time. With the randomness shim, `gen_random_uuid()` replays too. Without the shim, the rebuild diverges on it.
- A background job the agent started finishes during the model's reply in the original run. The replay waits for it before the agent reads its output, and without that wait the rebuild diverges.
- Parallel conversations (subagents) replayed in a different order each get their own replies. A request answered from the wrong conversation diverges.
- Real Claude Code in a container, live: the OAuth token is in Infisical and added by the proxy, so the container only has a placeholder.
  - The task called the MCP tool and ran Bash commands printing two `uuid4()`s, a `tempfile` name and `/dev/urandom` bytes.
  - The container was killed after model step 4. A fresh one replayed and checked steps 0–3, went live, and finished the task.
  - A full replay of all 8 steps in another fresh container, with no credential at all, matched every step. The four random outputs were byte-identical to the original run's.
  - An earlier live run also replayed a "you've been quiet a while" message that Claude Code inserts based on elapsed wall time. The pinned clock reproduces it.

Not done:
- harnesses other than Claude Code
- other model providers
- MCP servers over HTTP
- databases or MCP servers in other containers
- multi-host scheduling
- snapshots (rollout steps 4–5)

## MCP server state and databases

State is rebuilt, not restored: a fresh container starts with an empty `app_data/` and an empty database, and re-executing the recorded tool calls rebuilds both (and any in-memory MCP server state, since the servers restart and get the same calls in the same order). A write the original run committed but never recorded does not exist in the fresh environment. Three things keep that honest:

- **Every tool result is compared** with the recording, including ids and `created_at` (the database runs on the pinned clock).
- **State probes** (`--state-probe NAME=COMMAND`, run inside the environment before every model step) cover side effects no tool result shows: a probe's exit status and output hash are recorded and must replay. Without one, a tool that writes a different value but returns the same text passes silently; there is a test showing exactly that, and one showing the probe catching it. Probes must be deterministic: order query results, and hash logical dumps rather than files Postgres may rewrite.
- **Nothing carries state across attempts:** containers are removed with their anonymous volumes (the Postgres image keeps its data in one), `--volume NAME=PATH` volumes are recreated empty per attempt, and a named volume or writable host directory in `--docker-arg` is refused.

Not covered yet: databases or MCP servers in other containers (sidecars, compose services) are not reset between attempts; only single-container environments are supported. Use `--ready` so a database finishes booting before the harness starts: otherwise its boot races the first steps.

## Prototype decisions and limits

- **One recording per proxy** (`model.jsonl`, `mcp-<server>.jsonl`), so no two processes write the same file. Each proxy replays its own recording and goes live when it runs out. Recordings sit in the bind-mounted episode directory, so they outlive the container.
- **Model replies are matched by order**, with a shape check (model, stream flag, message count) and a check of the tool results the harness reports in each request (every message after the model's last turn: Claude Code sometimes appends its own message after the results). The latter covers Claude Code's built-in tools (Bash, Write), which never pass through the MCP proxy.
- **Streamed replies are buffered**, so a reply is fsynced before the harness sees any of it. The harness gets the stream all at once.
- **Model calls and MCP tool calls run one at a time**, in the original run too. A replayed request's assistant turns must match the recorded request's, so a request answered from the wrong recorded event diverges. Requests that arrived while another was in flight (parallel subagents) are recorded as overlapping. On rebuild, each is answered from the event it matches, in whatever order they arrive. During overlapping steps, clock pins and the order of MCP calls follow arrival order, so a replay can diverge there; it diverges visibly, never silently.
- **Transient model errors (408, 429, 5xx) and auth errors (401, 403) are not recorded**; the harness retries the first, and the second depend on the credential. Other errors are recorded, since the harness reacts to them.
- **Clock:** pinned per model step via libfaketime, frozen by default (wall time reads exactly the step's time; monotonic clocks, sleeps and timeouts are untouched; wall-clock elapsed time reads zero). `--clock advancing` lets it run on, at the cost of sub-second timestamps differing between runs. The container is created with the clock set, so the image's own services (Postgres) use it. The clock file lives inside the container: through Docker Desktop bind mounts an updated file reads as missing or stale. Claude Code needs libfaketime ≥ 0.9.11 (`FAKETIME_DISABLE_SHM`); Debian ships 0.9.10, so the example images build 0.9.14. File modification times are not faked.
- **Randomness:** `PYTHONHASHSEED` and `RESUMABLE_EVALS_SEED` are always set. The seed is kept in `episode.json`, and a resume with another seed is refused. With `--random-lib`, `librandpin.so` gives each process a ChaCha20 stream keyed by the seed, the step's pinned time and a per-step counter. Forked children take their own counter. Draws made while the environment boots count apart from the harness's.
  - **Pinned:** `getrandom`, `getentropy`, `arc4random`, `/dev/urandom`, and shared OpenSSL's `RAND_bytes`. That covers Python (`uuid4`, `random`, `tempfile`), Postgres `gen_random_uuid()` and `random()`, and trixie's `mktemp`.
  - **Not pinned:**
    - Node's `crypto` and `Math.random`: Node's OpenSSL is static and mixes the pid, thread id and time into its nonce.
    - bookworm's `mktemp(1)`, which uses the monotonic clock.
    - The order in which threads of one process draw.
  - **Fallbacks are reported.** A process that can't take a counter falls back to real randomness. It is listed under `random_fallbacks` in the attempt record.
- **Background jobs** (Docker): the container's processes are recorded as each reply comes back. On rebuild, a reply waits until the jobs that had exited by then have exited again. Jobs still running are not held to the same progress; the result checks catch any difference that shows.
- **Divergence** is reported in `<recording>.diverged.json` and in `episode.json`. By default the episode is then rerun from scratch (`reran_after_divergence: true`), with the diverged recordings kept in `diverged-<attempt>/`.
- **Docker Desktop for macOS:** the episode directory must be under a shared path (`$HOME`). **Linux:** set `--proxy-bind` to the bridge gateway (e.g. 172.17.0.1), not 0.0.0.0, which would expose the proxy and its credential to the network.

## Development

```sh
uv sync
uv run pytest
```
