# resumable-evals

Resume an eval episode that failed partway through, from its last completed step, without changing the harness or the agent.

Environments are offline and their tools deterministic, so nothing is snapshotted. Two proxies record every model call and tool call. After a failure, a fresh container re-executes the recorded tool calls in order and checks each result against the recording. The unmodified harness fast-forwards on recorded model replies, and at the failed step both proxies go live. Design: [docs/DESIGN.md](docs/DESIGN.md).

## Layout

| Path | What |
| --- | --- |
| `src/resumable_evals/recording.py` | Append-only, fsynced JSONL recording store (one file per episode) |
| `src/resumable_evals/compare.py` | Normalizes volatile fields and compares a re-executed tool result to its recording |
| `src/resumable_evals/rebuild.py` | Rebuild cursor: serves recorded model replies by step, checks re-executed tool results, decides when to go live |
| `src/resumable_evals/model_proxy.py` | Model API proxy (stub) |
| `src/resumable_evals/mcp_proxy.py` | MCP proxy (stub) |
| `docs/DESIGN.md` | Design doc |

## Status

Rollout step 1 (prototype) in progress. The recording store, comparison and rebuild cursor work and are tested. The two proxies are not written yet.

## Development

```sh
uv sync
uv run pytest
```
