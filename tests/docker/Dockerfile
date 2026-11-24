# Test eval environment: Python, libfaketime, the randomness shim, the fake
# harness and fake MCP server.
# Build context: tests/, plus --build-context randpin=src/resumable_evals/shim
FROM debian:bookworm-slim AS shim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev
COPY --from=randpin randpin.c /src/
RUN gcc -O2 -Wall -shared -fPIC -o /src/librandpin.so /src/randpin.c -ldl -lpthread

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends faketime \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s "$(find /usr/lib -name libfaketime.so.1 | head -1)" /usr/local/lib/libfaketime.so.1
COPY --from=shim /src/librandpin.so /usr/local/lib/librandpin.so
COPY fake_harness.py fake_mcp_server.py /opt/eval/
RUN mkdir -p /work/app_data
WORKDIR /work
CMD ["sleep", "infinity"]
