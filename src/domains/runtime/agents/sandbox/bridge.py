#!/usr/bin/env python3
"""沙箱内 TCP→Unix socket bridge。

bwrap --unshare-net 后沙箱内 127.0.0.1 无监听。
HTTP_PROXY 指向 127.0.0.1:{port}，agent 的 HTTP 客户端连接此端口。
本脚本在沙箱内监听 127.0.0.1:{port}，转发到 /proxy.sock（host 侧 NetworkProxy）。

用法：python3 bridge.py PORT &  exec agent_cmd
"""
import os
import select
import signal
import socket
import socketserver
import sys


UNIX_PATH = os.environ.get("PROXY_SOCK", "/proxy.sock")
MAX_CONNECTIONS = 50  # 连接上限——防止 agent 打爆

_active_connections = 0


class TCPToUnixHandler(socketserver.BaseRequestHandler):
    """TCP 连接 → Unix socket 转发。"""

    def handle(self):
        global _active_connections
        if _active_connections >= MAX_CONNECTIONS:
            print(f"bridge: max connections ({MAX_CONNECTIONS}) reached, rejecting",
                  file=sys.stderr)
            return
        _active_connections += 1
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(UNIX_PATH)
        except Exception as e:
            print(f"bridge: upstream connect failed: {e}", file=sys.stderr)
            return

        sockets = [self.request, upstream]
        while True:
            r, _, _ = select.select(sockets, [], [])
            for s in r:
                if s is self.request:
                    data = self.request.recv(65536)
                    if not data:
                        upstream.close()
                        return
                    upstream.sendall(data)
                else:
                    data = upstream.recv(65536)
                    if not data:
                        self.request.close()
                        return
                    self.request.sendall(data)

    def finish(self):
        global _active_connections
        _active_connections -= 1


class _BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    if len(sys.argv) < 2:
        print("usage: bridge.py PORT", file=sys.stderr)
        sys.exit(1)
    try:
        port = int(sys.argv[1])
        if not (1 <= port <= 65535):
            raise ValueError(f"port out of range: {port}")
    except ValueError as e:
        print(f"bridge: invalid port: {e}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(UNIX_PATH):
        print(f"bridge: {UNIX_PATH} not found, proxy disabled", file=sys.stderr)
        sys.exit(0)

    try:
        server = _BridgeServer(("127.0.0.1", port), TCPToUnixHandler)
    except Exception as e:
        print(f"bridge: bind failed on port {port}: {e}", file=sys.stderr)
        sys.exit(1)

    def _shutdown(signum, frame):
        server.shutdown()
    signal.signal(signal.SIGTERM, _shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
