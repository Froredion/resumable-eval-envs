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
| `src/resumable_evals/environments.py` | `DockerEnvironment` (a fresh container from the base image per attempt) and `DirEnvironment` (a fresh directory copy, for work without Docker) |
| `src/resumable_evals/orchestrator.py` | Runs an episode; on failure rebuilds a fresh environment and resumes; tags the result in `episode.json` |
| `examples/claude-code/` | Example eval image: Claude Code, a stateful MCP server, libfaketime 0.9.14 |
| `tests/docker/` | Test images, including one with Postgres |
| `docs/DESIGN.md` | Design doc |

## Usage

Run Claude Code in a container, with one MCP server, resuming on failure:

```sh
docker build -t eval-cc examples/claude-code
inf uv run python -m resumable_evals.orchestrator \
  --episode ~/runs/ep1 --image eval-cc --workdir /work \
  --mcp "counter=python /opt/eval/counter_mcp_server.py {workdir}/app_data" \
  --faketime-lib /usr/local/lib/libfaketime.so.1 \
  --oauth-token-env CLAUDE_CODE_OAUTH_TOKEN --env ANTHROPIC_API_KEY=placeholder \
  --timeout 1800 -- \
  claude -p "<task>" --mcp-config {mcp_config} --strict-mcp-config --allowedTools ...
```

The harness, the MCP proxies and the MCP servers run in the container; the model proxy runs on the host and adds the credential (from the host variable named by `--api-key-env` or `--oauth-token-env`), so the agent only ever sees the placeholder. `~/runs/ep1/episode.json` gets the status, every attempt, and `resumed` / `resumed_from_steps` for filtering. After a completed episode the container stays up for grading. `--fault-after-steps N --fault environment` kills the first attempt's container after N model steps, to try resume on demand. Without Docker, `--base DIR --workdir DIR` copies a directory fresh per attempt instead.

## Status

Rollout steps 1–3 (prototype, determinism controls, orchestrator) work, with Docker environments; 32 tests, three of them in Docker:

- A container killed mid-episode is rebuilt from its image and the episode finishes with nothing doubled; `date` inside read the pinned step time in both runs.
- A container running Postgres is killed mid-episode; on the fresh database the re-executed inserts return the same ids and `created_at`, each exactly its step's pinned time.
- Real Claude Code in a container, live (OAuth token from Infisical, added by the proxy; the container only has a placeholder): the container was killed after model step 4; a fresh one replayed and checked steps 0–3 (the MCP call and a Bash file write re-executed in it), went live, and finished the task with nothing doubled. A full replay of that episode in another fresh container matched all 8 steps with no model calls, including a "you've been quiet a while" message Claude Code inserts based on elapsed wall time, which the pinned clock reproduces.

Not done: harnesses other than Claude Code, other model providers, MCP servers over HTTP, multi-host scheduling, snapshots (steps 4–5).

## Prototype decisions and limits

- **One recording per proxy** (`model.jsonl`, `mcp-<server>.jsonl`), so no two processes write the same file. Each proxy replays its own recording and goes live when it runs out. Recordings sit in the bind-mounted episode directory, so they outlive the container.
- **Model replies are matched by order**, with a shape check (model, stream flag, message count) and a check of the tool results the harness reports in each request (every message after the model's last turn: Claude Code sometimes appends its own message after the results). The latter covers Claude Code's built-in tools (Bash, Write), which never pass through the MCP proxy.
- **Streamed replies are buffered**, so a reply is fsynced before the harness sees any of it. The harness gets the stream all at once.
- **Model calls and MCP tool calls run one at a time**, in the original run too. Parallel subagents with the same model and message count could be mis-paired on replay; avoid them for now.
- **Transient model errors (408, 429, 5xx) and auth errors (401, 403) are not recorded**; the harness retries the first, and the second depend on the credential. Other errors are recorded, since the harness reacts to them.
- **Clock:** pinned per model step via libfaketime, frozen by default (wall time reads exactly the step's time; monotonic clocks, sleeps and timeouts are untouched; wall-clock elapsed time reads zero). `--clock advancing` lets it run on, at the cost of sub-second timestamps differing between runs. The container is created with the clock set, so the image's own services (Postgres) use it. The clock file lives inside the container: through Docker Desktop bind mounts an updated file reads as missing or stale. Claude Code needs libfaketime ≥ 0.9.11 (`FAKETIME_DISABLE_SHM`); Debian ships 0.9.10, so the example images build 0.9.14. File modification times are not faked.
- **Randomness:** only `PYTHONHASHSEED` and `RESUMABLE_EVALS_SEED` are set. Pinning UUIDs and temp names is per environment and not done.
- **Divergence** is reported in `<recording>.diverged.json` and in `episode.json`. By default the episode is then rerun from scratch (`reran_after_divergence: true`), with the diverged recordings kept in `diverged-<attempt>/`.
- **Docker Desktop for macOS:** the episode directory must be under a shared path (`$HOME`). **Linux:** set `--proxy-bind` to the bridge gateway (e.g. 172.17.0.1), not 0.0.0.0, which would expose the proxy and its credential to the network.

## Development

```sh
uv sync
uv run pytest
```
