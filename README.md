# resumable-evals

[![CI](https://github.com/Froredion/resumable-eval-envs/actions/workflows/ci.yml/badge.svg)](https://github.com/Froredion/resumable-eval-envs/actions/workflows/ci.yml)

Resume an eval episode that failed partway through, from its last completed step, without changing the harness or the agent.

## Why this exists

AI data companies and research labs run agents through long, multi-step tasks in containerized environments, at scale. Many of those runs never finish cleanly. A container gets killed, a host goes down, a harness hangs or a network call times out, often hours into a task. By my estimate, around half the runs of many tasks at these companies end failed or botched.

Today the usual fix is to throw the run away and start the task over from step zero. That pays twice for everything that already worked:

- **Model calls:** every completed step is generated again, at full token cost.
- **Compute and wall-clock time:** the environment is rebuilt and every step is redone, including the slow ones.
- **The run itself:** a restart is a new trajectory, not the one that crashed, so the work already done is lost, not just delayed.

Simply restarting a container doesn't fix this. The agent's context lives in the harness, and processes, open connections and in-memory state don't survive a crash. A restored disk alone isn't the moment the run failed (see [the design doc](docs/DESIGN.md#problem)).

resumable-evals resumes the run instead. Every model call and tool call is recorded as it happens. After a failure, a fresh environment is rebuilt by replaying the recording. Model replies come from the recording, so replay makes no model calls and isn't charged against the episode's time limit. Tool calls are re-executed and every result is checked against the original. At most the one step in progress is lost. Resumed episodes are tagged, so they can be reported apart from uninterrupted ones. It works with unmodified harnesses and agents: they just talk to the proxies instead of the model API and MCP servers directly.

## How it works

Environments are offline and their tools deterministic, so nothing is snapshotted. Two proxies record every model call and tool call. After a failure, a fresh container re-executes the recorded tool calls in order and checks each result against the recording. The unmodified harness fast-forwards on recorded model replies, and at the failed step both proxies go live. Design: [docs/DESIGN.md](docs/DESIGN.md).

Status: alpha. Claude Code on the Anthropic Messages API, with stdio MCP servers, in single-container Docker environments.

## Requirements

- Python 3.12+ (the core has no runtime dependencies)
- Docker, for container environments: Docker Desktop or colima on macOS, Docker Engine on Linux
- An eval image with libfaketime ≥ 0.9.11 and, to pin random values, the `librandpin.so` shim built from `src/resumable_evals/shim` (see [`examples/claude-code/Dockerfile`](examples/claude-code/Dockerfile))

## Install

```sh
uv tool install git+https://github.com/Froredion/resumable-eval-envs   # or: pip install git+https://github.com/Froredion/resumable-eval-envs
resumable-evals --help
```

From a checkout: `uv sync`, then `uv run resumable-evals --help` (also `python -m resumable_evals`).

## Layout

| Path | What |
| --- | --- |
| `src/resumable_evals/recording.py` | Append-only, fsynced JSONL recording store (one file per proxy per episode) |
| `src/resumable_evals/compare.py` | Normalizes volatile fields and compares a re-executed result to its recording |
| `src/resumable_evals/rebuild.py` | Rebuild cursor: walks a recording, checks re-executed calls, decides when to go live |
| `src/resumable_evals/model_proxy.py` | Anthropic Messages API proxy: records replies, replays them on rebuild, checks the harness's own tool results, pins the clock per step, can supply the credential itself |
| `src/resumable_evals/mcp_proxy.py` | Stdio MCP proxy: records tool calls, re-executes and checks them on rebuild; runs on the host behind a TCP listener per server |
| `src/resumable_evals/relay.py` | The stdio "server" the harness launches inside the environment: relays to the MCP proxy on the host |
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
export CLAUDE_CODE_OAUTH_TOKEN=...   # stays on the host; the proxy adds it upstream
resumable-evals \
  --episode ~/runs/ep1 --image eval-cc --workdir /work \
  --mcp "counter=python /opt/eval/counter_mcp_server.py {workdir}/app_data" \
  --faketime-lib /usr/local/lib/libfaketime.so.1 --random-lib /usr/local/lib/librandpin.so \
  --oauth-token-env CLAUDE_CODE_OAUTH_TOKEN --env ANTHROPIC_API_KEY=placeholder \
  --volume pgdata=/var/lib/postgresql/data \
  --state-probe 'db=pg_dump -U postgres --data-only app | sha256sum' \
  --ready 'pg_isready -h 127.0.0.1' \
  --idle "test \"\$(redis-cli llen celery)\" = 0" \
  --timeout 1800 --attempt-timeout 3600 -- \
  claude -p "<task>" --mcp-config {mcp_config} --strict-mcp-config --allowedTools ...
```

The harness and the MCP servers run in the container. The proxies run on the host, and so do the recordings: nothing of the episode directory is mounted into the container, so the agent can't read its transcript or an earlier attempt's, nor change what a rebuild is checked against. The harness's MCP config launches a small relay (copied into the container) that connects to each server's MCP proxy; the proxy starts the real server in the container with `docker exec`. The model proxy adds the credential, so the agent only ever sees the placeholder.

`~/runs/ep1/episode.json` gets the status, every attempt, and `resumed` / `resumed_from_steps` for filtering. After a completed episode the container stays up for grading. Without Docker, `--base DIR --workdir DIR` copies a directory fresh per attempt instead.

On a Linux host, set `--proxy-bind` to the Docker bridge gateway (see [Security](#security)).

### Options

| Flag | What it does |
| --- | --- |
| `--api-key-env VAR` / `--oauth-token-env VAR` | Host variable holding the credential the model proxy adds upstream. It is removed from the orchestrator's environment and never enters the container. |
| `--mcp NAME=COMMAND` | An MCP server to start in the container behind its own proxy (repeat for more). `{workdir}` expands. `{mcp_config}` in the harness command expands to the generated MCP config. |
| `--faketime-lib PATH` | libfaketime inside the image; pins the clock per step. |
| `--random-lib PATH` | `librandpin.so` inside the image; pins random values per step. |
| `--clock frozen\|advancing` | Pinned clock mode (default frozen). |
| `--seed N` | Seed for the randomness shim and `PYTHONHASHSEED`; a resume with another seed is refused. |
| `--max-attempts N` | Attempts before the episode is marked failed (default 4). |
| `--volume NAME=PATH` | A volume recreated empty every attempt. |
| `--state-probe NAME=COMMAND` | Run before every model step; its exit status and output hash must replay (see below). |
| `--ready COMMAND` | Holds the harness until the image's own services are up, in every attempt. |
| `--settle-timeout S` | How long a replayed reply waits for background jobs (default 30, 0 to turn off). |
| `--idle COMMAND` | Succeeds when no queued background work is pending (the example checks a Celery queue in Redis); a rebuild waits for it wherever the original run was idle. |
| `--timeout S` | The episode's live working time, across attempts. Replay isn't charged, a resumed attempt carries on from the time already used, and running out ends the episode as `timed_out`. |
| `--attempt-timeout S` | Wall-clock limit per attempt, for hangs; retried. |
| `--fault-after-steps N --fault environment` | Kills the first attempt's container after N model steps, to try resume on demand. |

`resumable-evals --help` lists the rest.

## Status

Steps 1–3 of the [rollout plan](docs/DESIGN.md#rollout-plan) (prototype, determinism controls, orchestrator) work with Docker environments. There are 71 tests; 12 of them run in Docker:

- A container killed mid-episode is rebuilt from its image, and the episode finishes with nothing done twice. `date` inside the container read the pinned step time in both runs.
- A container running Postgres is killed mid-episode. On the fresh database, the re-executed inserts return the same ids and `created_at`, each exactly its step's pinned time. With the randomness shim, `gen_random_uuid()` replays too. Without the shim, the rebuild diverges on it.
- A background job the agent started finishes during the model's reply in the original run. The replay waits for it before the agent reads its output, and without that wait the rebuild diverges.
- Parallel conversations (subagents) replayed in a different order each get their own replies. A request answered from the wrong conversation diverges.
- An order queued for a background worker ships while the model thinks in the original run. Without `--idle` the replay cancels it first and diverges; with it the replay waits and matches.
- An agent searching the whole container for recordings, an earlier attempt's archive included, finds nothing, and the container has no bind mounts.
- Four 0.4 s steps against a 1.4 s limit: an uninterrupted run times out, and so does one that crashed and resumed.
- Real Claude Code in a container, live (run by hand, not in CI, since it needs a credential): the OAuth token stays on the host and is added by the proxy, so the container only has a placeholder.
  - The task called the MCP tool and ran Bash commands printing two `uuid4()`s, a `tempfile` name and `/dev/urandom` bytes.
  - The container was killed after model step 4. A fresh one replayed and checked steps 0–3, went live, and finished the task.
  - A full replay of all 8 steps in another fresh container, with no credential at all, matched every step. The four random outputs were byte-identical to the original run's.
  - A second live run, after the recordings moved out of the container: the agent's MCP calls went through the relay, and a Bash `find /` for `*.jsonl` and `episode.json` in the rebuilt container found only Claude Code's own session file (`/root/.claude/projects/...`, the conversation it is in), none of ours. Killed after step 4 and resumed, the episode's live time carried straight across the crash. A full replay with no credential matched all 8 steps, its random outputs were byte-identical, and the replay added no live time.
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

- **One recording per proxy** (`model.jsonl`, `mcp-<server>.jsonl`), so no two processes write the same file. Each proxy replays its own recording and goes live when it runs out. Recordings are written on the host, outside the container.
- **Isolation limits:** the agent can still reach the proxies themselves. Calls it makes to them are recorded like the harness's. The model proxy forwards only model calls, token counting and the model list; it refuses other endpoints, which are listed under `model_api_refused` in the attempt record. Each MCP server accepts one connection per attempt; a second one is refused and noted under `mcp_connections_refused`. The clock file and random counters are inside the container and writable. `DirEnvironment` runs on the host and isolates nothing.
- **Model replies are matched by order**, with a shape check (model, stream flag, message count) and a check of the tool results the harness reports in each request (every message after the model's last turn: Claude Code sometimes appends its own message after the results). The latter covers Claude Code's built-in tools (Bash, Write), which never pass through the MCP proxy.
- **Streamed replies are buffered**, so a reply is fsynced before the harness sees any of it. The harness gets the stream all at once.
- **Model calls and MCP tool calls run one at a time**, in the original run too. A replayed request's assistant turns must match the recorded request's, so a request answered from the wrong recorded event diverges. Requests that arrived while another was in flight (parallel subagents) are recorded as overlapping. On rebuild, each is answered from the event it matches, in whatever order they arrive. During overlapping steps, clock pins and the order of MCP calls follow arrival order, so a replay can diverge there; it diverges visibly, never silently.
- **Transient model errors (408, 429, 5xx) and auth errors (401, 403) are not recorded**; the harness retries the first, and the second depend on the credential. Other errors are recorded, since the harness reacts to them.
- **Clock:** pinned per model step via libfaketime, frozen by default (wall time reads exactly the step's time; monotonic clocks, sleeps and timeouts are untouched; wall-clock elapsed time reads zero). `--clock advancing` lets it run on, at the cost of sub-second timestamps differing between runs. The container is created with the clock set, so the image's own services (Postgres) use it. The clock file lives inside the container: through bind mounts from a macOS host (Docker runs in a Linux VM there) an updated file reads as missing or stale. Claude Code needs libfaketime ≥ 0.9.11 (`FAKETIME_DISABLE_SHM`); Debian ships 0.9.10, so the example images build 0.9.14. File modification times are not faked.
- **Randomness:** `PYTHONHASHSEED` and `RESUMABLE_EVALS_SEED` are always set. The seed is kept in `episode.json`, and a resume with another seed is refused. With `--random-lib`, `librandpin.so` gives each process a ChaCha20 stream keyed by the seed, the step's pinned time and a per-step counter. Forked children take their own counter. Draws made while the environment boots count apart from the harness's.
  - **Pinned:** `getrandom`, `getentropy`, `arc4random`, `/dev/urandom`, and shared OpenSSL's `RAND_bytes`. That covers Python (`uuid4`, `random`, `tempfile`), Postgres `gen_random_uuid()` and `random()`, and trixie's `mktemp`.
  - **Not pinned:**
    - Node's `crypto` and `Math.random`. Observed with libfaketime and the shim both loaded: same seed, different values. `node` binds `getrandom`/`getentropy` to the shim and draws from it once per process, so its own generator varies for another reason, not yet established.
    - bookworm's `mktemp(1)`, likewise observed. It binds no random source, only `clock_gettime`.
    - The order in which threads of one process draw.
  - **Fallbacks are reported.** A process that can't take a counter falls back to real randomness. It is listed under `random_fallbacks` in the attempt record.
- **Background jobs** (Docker): the container's processes are recorded as each reply comes back. On rebuild, a reply waits until the jobs that had exited by then have exited again. Jobs still running are not held to the same progress; the result checks catch any difference that shows.
- **Queued work** (`--idle`): a queue worker never exits, so the process wait doesn't cover it. The idle check's result is recorded as each reply comes back, and a reply recorded idle is held until the check passes again. Where the original run was busy, nothing is held: partial progress can't be reproduced. Pair it with a state probe on the tables the jobs change. Example checks, which the environment's author adapts:
  - Celery with Redis: `test "$(redis-cli llen celery)" = 0`.
  - Sidekiq: `test "$(redis-cli llen queue:default)" = 0`.
  - A database job table: `test "$(psql -qAt -c "SELECT count(*) FROM jobs WHERE status IN ('pending', 'running')")" = 0`.

  The two Redis checks count only jobs still queued. A job a worker has already picked up is not in the list, so also check that none is running (for Celery, `celery -A app inspect active`).
- **Time limit:** each recorded model reply carries the episode's live working time so far, fsynced with it. A resumed attempt starts counting once its replay is used up, so replay and the step lost in the failure aren't charged. A model call already in flight when the limit passes still completes.
- **Divergence** is reported in `<recording>.diverged.json` and in `episode.json`. By default the episode is then rerun from scratch (`reran_after_divergence: true`), with the diverged recordings kept in `diverged-<attempt>/`.
- **Linux hosts:** see [Security](#security).

## Security

The model proxy holds the credential and forwards model calls to the provider, so treat its address as sensitive:

- **Linux hosts:** set `--proxy-bind` to the Docker bridge gateway (`docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'`, often 172.17.0.1). Never 0.0.0.0, which would expose the proxy and its credential to the network. On macOS the default 127.0.0.1 works.
- Recordings hold full model transcripts and tool results. Keep episode directories private if your tasks are.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```sh
uv sync
uv run pytest          # 71 tests; the 12 Docker tests skip without Docker
uv run ruff check .
```

On a Linux host, the Docker tests need `RESUMABLE_EVALS_TEST_PROXY_BIND` set to the bridge gateway, as in CI. See [CONTRIBUTING.md](CONTRIBUTING.md) before changing record or rebuild behaviour.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
