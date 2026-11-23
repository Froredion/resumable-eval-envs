# Test eval environment: Python, libfaketime, the fake harness and fake MCP server.
# Build context: tests/
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends faketime \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s "$(find /usr/lib -name libfaketime.so.1 | head -1)" /usr/local/lib/libfaketime.so.1
COPY fake_harness.py fake_mcp_server.py /opt/eval/
RUN mkdir -p /work/app_data
WORKDIR /work
CMD ["sleep", "infinity"]
