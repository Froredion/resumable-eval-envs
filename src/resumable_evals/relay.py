"""MCP relay: the stdio server the harness launches inside the environment.

It connects to the MCP proxy on the host and copies bytes both ways, so the
proxy, and the recording it writes, stay outside the environment, where the
agent can neither read nor change them. The orchestrator copies this file
into the environment and names it in the harness's MCP config::

    python relay.py HOST PORT

Standard library only, Python 3.8+: it runs on whatever Python the image has.
"""

import socket
import sys
import threading


def main() -> int:
    host, port = sys.argv[1], int(sys.argv[2])
    conn = socket.create_connection((host, port))

    def upstream() -> None:
        stdin = sys.stdin.buffer
        while True:
            data = stdin.read1(65536)
            if not data:
                break
            conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)  # the harness closed the server: so does the proxy

    threading.Thread(target=upstream, daemon=True).start()
    out = sys.stdout.buffer
    while True:
        data = conn.recv(65536)
        if not data:
            return 0
        out.write(data)
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
