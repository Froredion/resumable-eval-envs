# Resumable AI Eval Environments

Oct 29, 2026 · @Erickson

## Summary

We want to restart any eval environment that fails mid-episode and continue from the last completed step, without changing any harness or agent. Because eval environments are offline and their tools deterministic, we don't need to save the environment: we rebuild it. Two proxies record every model call and tool call; on failure, a fresh environment re-executes the recorded tool calls in order, each result is checked against the recording, and the harness fast-forwards on recorded model replies until it reaches the failed step.

At most one in-progress step is lost and retried. Snapshots are optional, kept only for very long episodes or as a fallback when a re-executed result diverges. New harnesses, agents and MCP servers plug in by pointing their endpoints at the proxies, not by code changes.

## Problem

Saving the disk is easy; saving the episode is not. Postgres, files and container images can all be backed up, but an eval run also depends on state that never touches disk.

| What is lost on restart | Why it matters |
| --- | --- |
| Running processes, RAM, open sockets, in-flight transactions | The agent's servers, shells and DB sessions are gone even though files are back |
| Agent context (transcript, tool history, step count, budget) | Lives in the harness, not the container; must match the restored environment exactly |
| Consistency of a live Postgres copy | A filesystem copy of a running DB can be corrupt without an atomic snapshot or WAL-based backup |
| External services and time | Clocks jump, tokens expire, remote APIs move on; none of it rolls back with the container |
| Stateful MCP servers | Sessions and in-memory state sit outside the main container |

Today the only safe response to a mid-episode failure is to discard the run and start over, which wastes compute on long tasks.

## Requirements and constraints

The system must work across everything we run without per-harness integration.

- **Harness-agnostic:** multiple harnesses run today and more will come; none can be assumed to support pause or resume.
- **Agent-agnostic:** different agents and model providers.
- **MCP-aware:** agents use multiple MCP servers, some of them stateful.
- **No lost progress:** a failure loses at most the step in progress.
- **Consistent restore:** containers, Postgres, volumes, MCP server state and agent transcript all return to the same moment.
- **Auditable:** every resumed episode is marked as resumed, with the checkpoint it came from, so results can be filtered or compared.
- **Low overhead:** checkpointing should add seconds per step, not minutes; frequency is configurable.

### Assumptions

- **Environments are offline.** No package registries, external APIs or remote data, so nothing outside the container changes between runs.
- **Tools and MCP servers are deterministic by design.** The same calls in the same order produce the same results and the same state.
- **MCP servers persist state under `app_data/` on the filesystem**, in formats we don't know or control. Re-execution rebuilds it, so we never need to read or export it.
- **Environments run as Docker containers, at scale** (many episodes in parallel across many hosts).

What this means for the design:

- Restoring an episode = a fresh container from the base image + re-executing recorded tool calls. No snapshot storage is needed on the main path.
- The remaining sources of drift are inside the container: the clock, random values (UUIDs, temp names, salts), and commands the agent runs itself through bash. Determinism controls (below) pin these.
- A per-step comparison of re-executed results against the recording catches anything that slips through.
- Optional snapshots, if enabled, keep all writable state (workspace, Postgres data, `app_data/`) on volumes on a copy-on-write filesystem (ZFS or btrfs), taken with containers paused. `docker commit` is avoided: it is slow, grows images, and misses volumes.

## Architecture

Harnesses never talk to models or tools directly; every call passes through the proxy layer, which is what makes resume harness-agnostic.

&#91;embedded content: architecture · proxy layer, checkpointing, orchestrator\]

- **Model proxy:** all model API traffic goes through it. It records the full transcript for any harness, and on rebuild answers model requests from the recording so the harness fast-forwards.
- **MCP proxy:** agents connect to it instead of directly to MCP servers. It records tool calls and results, and on rebuild re-executes each recorded call against the fresh environment and checks the result.
- **Recording store:** durable storage for model replies and tool calls, written as they happen so they survive a crash.
- **Orchestrator:** detects failures, starts a fresh environment, restarts the harness, switches the proxies between rebuild and live mode, and flags divergence.
- **Snapshots (optional):** copy-on-write volume snapshots every N steps, only for environments with very long episodes.

## Record and rebuild flow

A failed episode is rebuilt by re-executing its recorded tool calls on a fresh environment, not by restoring a saved one.

**Normal run (record mode)**

1. The harness sends a model request; the model proxy forwards it and records the request and reply, numbered by step.
2. Tool calls pass through the MCP proxy, which forwards them and records each call and result in order.
3. Recordings are written to durable storage as they happen, so they survive the environment crashing.

**On failure (rebuild mode)**

1. The orchestrator detects the failure (crash, health check, timeout, harness error).
2. It tears down the environment and starts a fresh one from the base image, with the same seeds and pinned clock.
3. It restarts the harness from the beginning, unmodified.
4. The model proxy answers each model request from the recording, so the harness fast-forwards and rebuilds its own internal state.
5. The MCP proxy **re-executes** each recorded tool call against the fresh environment and compares the result to the recording. A match means the environment is in the same state as the original run.
6. At the failed step, both proxies switch to live mode and the episode continues.
7. The episode is tagged as resumed, with the step it resumed from.

If a re-executed result does not match the recording, the rebuild stops and the episode is flagged as diverged. It is then rerun from scratch, or restored from a snapshot if snapshots are enabled for that environment.

### Determinism controls

The tools are deterministic; these controls cover what the agent and the OS can still vary.

- **Clock:** a fake clock (libfaketime) pinned to the recorded time of each step, so logs, file times and `created_at` columns match.
- **Randomness:** fixed seeds for the environment, and pinned sources for UUIDs and temp names where tools or the agent generate them.
- **Agent-run commands:** commands the agent runs through bash (`date`, `uuidgen`) get the same clock and seeds.
- **Ordering:** tool calls are re-executed one at a time, in recorded order, waiting for background jobs to settle where the original run did.

### Optional snapshots

For very long episodes, where re-execution would take too long, the environment can also be snapshotted every N steps. Rebuild then starts from the latest snapshot and re-executes only the tool calls after it.

## Risks and edge cases

The main risk is a rebuilt environment that silently differs from the original; the per-step result check turns that into a visible, flagged divergence.

| Risk | Mitigation |
| --- | --- |
| A re-executed tool result differs from the recording (clock, random value, ordering) | Compare every result; stop and flag on mismatch; rerun from scratch or from a snapshot; fix the source with a determinism control |
| Divergence that doesn't show in tool results (a side effect the tool output doesn't report) | Optionally hash key state (workspace files, `app_data/`, DB tables) at recorded steps and compare on rebuild |
| Harness adds timestamps or random IDs to prompts, so model requests don't match recordings | Match model replies by step order, not exact content; normalize known volatile fields |
| Harness runs commands directly in the container, bypassing the MCP proxy | Record those commands too (shell wrapper or exec hook), or the rebuild misses their effects |
| Background processes started by the agent | Re-executing the command that started them starts them again; wait for them to settle at recorded points |
| Long episodes make re-execution slow | Optional snapshots every N steps; rebuild re-executes only after the latest snapshot |
| Resumed episodes scoring differently from uninterrupted ones | Tag resumed episodes; report results with and without them; track resume and divergence rate per task |
| Model provider streaming or provider-specific APIs | Proxy records full streamed responses; one adapter per provider |

## Rollout plan

Start with record and rebuild for one harness; snapshots come last and only if long episodes need them. Without snapshot infrastructure on the critical path, a working prototype is smaller than first planned.

1. **Prototype:** model proxy and MCP proxy with recording; rebuild by re-execution with per-step result checks; one harness, one model provider, one environment.
2. **Determinism controls:** fake clock pinned to recorded times, seeded randomness, capture of agent-run shell commands.
3. **Orchestrator:** failure detection, automatic rebuild, divergence flagging, resumed-episode tagging.
4. **Widen coverage:** remaining harnesses, model providers and environments; track divergence rate per environment to find remaining non-determinism.
5. **Optional snapshots:** copy-on-write volume snapshots every N steps, only for environments where episodes are long enough that re-execution is too slow.

## What the build learned

Rollout steps 1–3 are built (status and usage in the README). Building them refined the design in these places:

- **The harness's own tools are checked in the next model request.** Claude Code runs Bash, Write and Edit itself, so they never pass through the MCP proxy. Their results reach the model in the next request, and the model proxy compares every tool result after the model's last turn with the recording. No shell wrapper or exec hook is needed to check them.
- **Replies are matched by order and checked by content.** A replayed request must have the recorded shape (model, stream flag, message count). Its assistant turns must also match exactly: they are the harness's rendering of recorded replies, so a request answered from the wrong recorded event diverges. Some requests arrive while another is still in flight (parallel subagents). These are recorded as overlapping, and on rebuild each is answered from the event it matches, in whatever order they arrive.
- **Clock:** libfaketime, frozen at each step's pinned time by default. Wall-clock time reads exactly the step's time, and monotonic clocks are untouched, so sleeps and timeouts still work. The container boots with the clock already pinned, so its own services (Postgres) run on it. The clock file lives inside the container and is written with `docker exec`: through Docker Desktop's bind mounts, an updated file reads as missing or stale. Claude Code needs libfaketime 0.9.11 or later.
- **Randomness:** a preload shim (`librandpin.so`) replaces libc's random sources and shared OpenSSL's `RAND_bytes` with a stream per process. The stream is keyed by the seed, the step's pinned time and a per-step counter.
  - **Pinned:** Python (`uuid4`, `random`, `tempfile`), `/dev/urandom` readers, glibc's `arc4random`, and Postgres `gen_random_uuid()` and `random()`.
  - **Not pinned:**
    - A statically linked OpenSSL. Node's `crypto` mixes the pid, thread id and time into its nonce.
    - Tools that derive names from the monotonic clock, such as Debian bookworm's `mktemp`.

    These show up as divergences.
  - **Boot is kept apart.** Draws made while the environment boots are counted separately from the harness's. `--ready` holds the harness until the environment's services are up. Together these stop a boot of varying length from shifting the harness's random values.
- **Background jobs:** the container's processes are recorded as each model reply comes back. On rebuild, the reply is held until every job that had exited by that point in the original run has exited again. This means the agent reads a job's output at the same stage in both runs.
- **State probes** cover side effects no tool result shows, such as a database row written with a different value. A probe is a command run every step whose output hash must replay.
- **The proxy adds the credential**, so it never enters the container. Auth failures (401, 403) and transient ones (408, 429, 5xx) are not recorded.
- **Nothing carries state between attempts:**
  - Containers are removed with their anonymous volumes.
  - Named volumes are recreated empty.
  - Writable host mounts are refused.

## Open questions

- [ ] What schedules containers across hosts at scale (plain Docker hosts, Swarm, Nomad, Kubernetes)?
- [ ] Which harnesses and model providers need support first?
- [x] Which tools or MCP servers read the clock or generate random values that need pinning? *Partly answered:*
  - Anything that reads the clock, `getrandom`, `/dev/urandom` or a shared OpenSSL is now pinned.
  - Node's `crypto` and monotonic-clock temp names are not.
  - Per-environment divergence rates will show what else matters.
- [x] Do harnesses run commands directly in containers, or only through MCP? *Yes:* Claude Code runs Bash, Write and Edit itself. Their results are checked in the next model request.
- [ ] Typical episode length and step count, to set checkpoint frequency?
- [ ] Should resumed episodes count in headline results, or be reported separately?
