# resumable-evals

Resume an eval episode that failed partway through, from its last completed step, without changing the harness or the agent.

Environments are offline and their tools deterministic, so nothing is snapshotted. Two proxies record every model call and tool call. After a failure, a fresh container re-executes the recorded tool calls in order and checks each result against the recording. The unmodified harness fast-forwards on recorded model replies, and at the failed step both proxies go live. Design: [docs/DESIGN.md](docs/DESIGN.md).

## Layout

| Path | What |
| --- | --- |
| `src/resumable_evals/recording.py` | Append-only, fsynced JSONL recording store (one file per proxy per episode) |
| `src/resumable_evals/compare.py` | Normalizes volatile fields and compares a re-executed result to its recording |
| `src/resumable_evals/rebuild.py` | Rebuild cursor: walks a recording, checks re-executed calls, decides when to go live |
| `src/resumable_evals/model_proxy.py` | Anthropic Messages API proxy: records replies, replays them on rebuild, checks the harness's own tool results, pins the clock per step |
| `src/resumable_evals/mcp_proxy.py` | Stdio MCP proxy: records tool calls, re-executes and checks them on rebuild |
| `src/resumable_evals/clock.py` | Per-step clock pinning through libfaketime's timestamp file |
| `src/resumable_evals/orchestrator.py` | Runs an episode; on failure rebuilds a fresh environment and resumes; tags the result in `episode.json` |
| `docs/DESIGN.md` | Design doc |

## Usage

Run Claude Code on an episode, with one MCP server, resuming on failure:

```sh
uv run python -m resumable_evals.orchestrator \
  --episode runs/ep1 --base envs/task1 --workdir /work/ep1 \
  --mcp "fs=fs-server --root {workdir}" --timeout 1800 -- \
  claude -p "<task>" --mcp-config {mcp_config} --strict-mcp-config --allowedTools ...
```

`runs/ep1/episode.json` gets the status, every attempt, and `resumed` / `resumed_from_steps` for filtering. `--fault-after-steps N` kills the first attempt after N model steps, to try resume on demand.

## Status

Rollout steps 1–3 (prototype, determinism controls, orchestrator) work locally and are tested (26 tests). With real Claude Code (OAuth login) an episode killed mid-run was rebuilt and finished with no step doubled, and a Bash result that differed on rebuild was caught as a divergence. Not done: Docker environments (`DirEnvironment` stands in for a container), harnesses other than Claude Code, other model providers, snapshots (steps 4–5).

## Prototype decisions and limits

- **One recording per proxy** (`model.jsonl`, `mcp-<server>.jsonl`), so no two processes write the same file. Each proxy replays its own recording and goes live when it runs out.
- **Model replies are matched by order**, with a shape check (model, stream flag, message count) and a check of the tool results the harness reports in each request. The latter is what covers Claude Code's built-in tools (Bash, Write), which never pass through the MCP proxy.
- **Streamed replies are buffered**, so a reply is fsynced before the harness sees any of it. The harness gets the stream all at once.
- **Model calls and MCP tool calls run one at a time**, in the original run too. Parallel subagents with the same model and message count could be mis-paired on replay; avoid them for now.
- **Transient model errors (408, 429, 5xx) are not recorded**; the harness retries them. Other errors are recorded, since the harness reacts to them.
- **Clock:** pinned per model step via libfaketime (checked in a Debian container). File modification times are not faked. On Docker Desktop for macOS the clock file must be in a shared directory (under `$HOME`).
- **Randomness:** only `PYTHONHASHSEED` and `RESUMABLE_EVALS_SEED` are set. Pinning UUIDs and temp names is per environment and not done.
- **Divergence** is reported in `<recording>.diverged.json` and in `episode.json`. By default the episode is then rerun from scratch (`reran_after_divergence: true`), with the diverged recordings kept in `diverged-<attempt>/`.

## Development

```sh
uv sync
uv run pytest
```
