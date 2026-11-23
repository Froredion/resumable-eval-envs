# Example eval environment for Claude Code: the agent, its tools and a
# stateful MCP server (counter under /work/app_data) in one container.
# Build context: this directory.

# libfaketime >= 0.9.11 for FAKETIME_DISABLE_SHM: Claude Code's executable
# re-execs itself under the same PID, and 0.9.10 (Debian bookworm/trixie)
# then fails creating its per-PID semaphore and exits.
FROM node:22-bookworm-slim AS libfaketime
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates
RUN git clone --depth 1 --branch v0.9.14 https://github.com/wolfcw/libfaketime /src \
 && make -C /src/src libfaketime.so.1

FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python-is-python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=libfaketime /src/src/libfaketime.so.1 /usr/local/lib/libfaketime.so.1
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force
COPY counter_mcp_server.py /opt/eval/
RUN mkdir -p /work/app_data
WORKDIR /work
CMD ["sleep", "infinity"]
