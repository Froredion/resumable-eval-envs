# Test eval environment with a database: Postgres (started by the image's
# own entrypoint, under the pinned clock), Python, the fake harness and an
# MCP server that writes to the database. Build context: tests/
FROM debian:bookworm-slim AS libfaketime
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates
RUN git clone --depth 1 --branch v0.9.14 https://github.com/wolfcw/libfaketime /src \
 && make -C /src/src libfaketime.so.1

FROM postgres:17-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python-is-python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=libfaketime /src/src/libfaketime.so.1 /usr/local/lib/libfaketime.so.1
COPY fake_harness.py pg_mcp_server.py /opt/eval/
ENV POSTGRES_HOST_AUTH_METHOD=trust
RUN mkdir -p /work && chmod 777 /work
WORKDIR /work
