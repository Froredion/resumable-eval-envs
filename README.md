# resumable-evals

Resume an eval episode that failed partway through, from its last completed step, without changing the harness or the agent.

Environments are offline and their tools deterministic, so nothing is snapshotted. Two proxies record every model call and tool call. After a failure, a fresh container re-executes the recorded tool calls in order and checks each result against the recording. The unmodified harness fast-forwards on recorded model replies, and at the failed step both proxies go live. Design: [docs/DESIGN.md](docs/DESIGN.md).

## Layout

| Path | What |
| --- | --- |
| `src/resumable_evals/recording.py` | Append-only, fsynced JSONL recording store (one file per episode) |
| `src/resumable_evals/compare.py` | Normalizes volatile fields and compares a re-executed tool result to its recording |
| `src/resumable_evals/rebuild.py` | Rebuild cursor: serves recorded model replies by step, checks re-executed tool results, decides when to go live |
| `src/resumable_evals/model_proxy.py` | Model proxy for the Anthropic Messages API: records replies, replays them in order on rebuild |
| `src/resumable_evals/mcp_proxy.py` | MCP proxy for stdio servers: records tool calls, re-executes and checks them on rebuild |
| `docs/DESIGN.md` | Design doc |

## Status

Rollout step 1 (prototype) in progress. The recording store, comparison, rebuild cursor, stdio MCP proxy and Anthropic model proxy work and are tested; the model proxy has recorded and replayed a real Claude Code session. The orchestrator (rollout step 3) and determinism controls (step 2) are not written yet.

## Development

```sh
uv sync
uv run pytest
```
