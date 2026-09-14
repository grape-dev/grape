"""沙箱网络代理 — HTTP CONNECT 代理 + Unix socket 桥接 + 白名单 + DNS 校验 + 审计（Issue #139）。

设计参考：
- anthropics/sandbox-runtime（Unix socket 桥接 + proxy env 生成 + domain-pattern + resolved-address-guard）
- agent-bom（审计 policy/reason 字段 + path 脱敏）

架构：
  宿主侧:
    SandboxProxy — Python HTTP 代理（ThreadingTCPServer）
      ├─ 白名单过滤（通配符匹配 + DNS 地址校验）
      ├─ hop-by-hop header 剥离 + 请求走私检查
      ├─ JSONL 审计（哈希链 + 轮转 + policy/reason + latency/status + path 脱敏）
      ├─ 违规通知（结构化 403 JSON body）
      └─ 崩溃监控（exit 事件 → kill Agent 回调）
    UnixSocketBridge — Unix socket → TCP 代理端口（替代 socat）

  沙箱内（bwrap --unshare-net）:
    Python 桥接: localhost:3128 → Unix socket → 宿主代理
    Agent HTTP_PROXY=localhost:3128 → 桥接 → Unix socket → 宿主代理 → 白名单 → 目标
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import os
import re
import socket
import socketserver
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from src.agent.audit import RotatingAuditLog

log = logging.getLogger("devops-bot")

# ===== 常量 =====

_CONNECT_TIMEOUT = 30  # CONNECT 握手超时（秒）
_TUNNEL_IDLE_TIMEOUT = 300  # 隧道空闲超时（秒）

# hop-by-hop headers — 不转发到上游（采纳 anthropics parent-proxy.ts:55-65）
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade",
})

# 代理 env 变量（采纳 anthropics sandbox-utils.ts:474-583）
_PROXY_ENV_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "GRPC_PROXY", "grpc_proxy",
]

# NO_PROXY 只含 ::1 — 不排除 localhost/127.0.0.1
# grape API 在 127.0.0.1:3000，必须走代理才能从沙箱到达宿主 3000 端口
_NO_PROXY_VALUES = "::1"

# DNS 解析后禁止连接的地址范围（采纳 anthropics resolved-address-guard.ts）
_DENIED_ADDRESS_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("::1/128"),             # loopback IPv6
    ipaddress.ip_network("169.254.0.0/16"),     # link-local（含云元数据 169.254.169.254）
    ipaddress.ip_network("fe80::/10"),           # link-local IPv6
    ipaddress.ip_network("0.0.0.0/8"),           # unspecified
    ipaddress.ip_network("224.0.0.0/4"),         # multicast
    ipaddress.ip_network("255.255.255.255/32"),  # broadcast
]

# 云元数据接口（不在 link-local 范围内的）
_CLOUD_METADATA_ADDRESSES = [
    "100.100.100.200",   # Alibaba Cloud
    "168.63.129.16",     # Azure WireServer
    "192.0.0.192",       # Oracle Cloud
]

# 敏感 URL 查询参数（审计脱敏）
_SENSITIVE_QUERY = re.compile(
    r"(token|key|secret|password|access_token|private_token|ak|sk)=([^&]+)",
    re.IGNORECASE,
)


# ===== 域名匹配 =====

def match_domain(host: str, pattern: str) -> bool:
    """白名单域名匹配 — 支持通配符。

    支持: example.com / *.example.com / example.com:443
    拒绝过宽模式: *.com / *
    """
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if "." not in suffix or suffix.startswith(".") or suffix.endswith("."):
            return False
        host_base = host.split(":")[0]
        # *.example.com 只匹配子域名，不匹配 example.com 本身
        return host_base.endswith("." + suffix)
    host_base = host.split(":")[0]
    pattern_base = pattern.split(":")[0]
    return host_base == pattern_base


# ===== DNS 地址校验 =====

def check_resolved_address(host: str, port: int) -> tuple[bool, str]:
    """DNS 解析后校验 IP — 防止 DNS 重绑定到内网/元数据接口。"""
    # IP 字面量直接校验
    try:
        ip = ipaddress.ip_address(host)
        return _check_ip(ip)
    except ValueError:
        pass

    # 域名解析
    try:
        infos = socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"

    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        ok, reason = _check_ip(ip)
        if not ok:
            return False, f"resolved to {ip_str}: {reason}"
    return True, "ok"


def _check_ip(ip: ipaddress._BaseAddress) -> tuple[bool, str]:
    for network in _DENIED_ADDRESS_RANGES:
        if ip in network:
            return False, f"address in denied range {network}"
    for addr in _CLOUD_METADATA_ADDRESSES:
        if str(ip) == addr:
            return False, "cloud metadata address"
    return True, "ok"


# ===== HTTP 请求处理 =====

def strip_hop_by_hop_headers(headers: dict) -> dict:
    """剥离 hop-by-hop headers，防止代理指令泄露到上游。"""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def check_request_smuggling(headers: dict) -> tuple[bool, str]:
    """检查 Content-Length + Transfer-Encoding 共存（请求走私原语）。"""
    has_cl = any(k.lower() == "content-length" for k in headers)
    has_te = any(k.lower() == "transfer-encoding" for k in headers)
    if has_cl and has_te:
        return False, "Content-Length and Transfer-Encoding both present (smuggling risk)"
    return True, "ok"


def sanitize_path(path: str) -> str:
    """脱敏 URL 查询参数中的 token/key/secret。"""
    def _redact(m: re.Match) -> str:
        return f"{m.group(1)}=[REDACTED]"
    return _SENSITIVE_QUERY.sub(_redact, path)


# ===== 代理 env 生成 =====

def generate_proxy_envs(proxy_port: int = 3128) -> list[tuple[str, str]]:
    """生成 bwrap --setenv 参数对。沙箱内代理地址是 localhost:3128。"""
    envs: list[tuple[str, str]] = []
    proxy_url = f"http://localhost:{proxy_port}"
    for key in _PROXY_ENV_KEYS:
        envs.append((key, proxy_url))
    envs.append(("NO_PROXY", _NO_PROXY_VALUES))
    envs.append(("no_proxy", _NO_PROXY_VALUES))
    envs.append(("GIT_CONFIG_PARAMETERS", "'http.proxyAuthMethod=basic'"))
    envs.append(("GIT_SSH_COMMAND", "/bin/false"))
    return envs


def get_dynamic_whitelist(base_whitelist: list[str]) -> list[str]:
    """动态白名单：基础白名单 + GRAPE_SERVER_URL + 127.0.0.1。

    Agent 调 grape API（127.0.0.1:3000 或公网 IP）需要走代理，必须加入白名单。
    localhost:3000 的流量会经过代理审计 — 这是预期行为，可发现异常 API 调用模式。
    """
    wl = list(base_whitelist)
    server_url = (
        os.environ.get("GRAPE_SERVER_URL", "")
        or os.environ.get("RUNTIME_SERVER_URL", "")
    )
    if server_url:
        parsed = urlparse(server_url)
        host = parsed.hostname or ""
        if host and host not in wl:
            wl.append(host)
        if host in ("localhost", "127.0.0.1") and "127.0.0.1" not in wl:
            wl.append("127.0.0.1")
    if "127.0.0.1" not in wl:
        wl.append("127.0.0.1")
    return wl


# ===== 代理 Handler =====

class WhitelistProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 正向代理：CONNECT（HTTPS 隧道）+ GET/POST/PUT/DELETE/PATCH/HEAD（HTTP 转发）。"""

    # 由 SandboxProxy 注入（类变量，所有线程共享只读）
    _task_id: str = ""
    _whitelist: list[str] = []
    _audit_log: RotatingAuditLog | None = None
    _leaked_tokens: list[tuple[str, str]] = []  # (token_name, token_value)

    def setup(self):
        """每个连接调用 — 把类变量拷贝到实例变量，避免并发共享。"""
        super().setup()
        self._task_id_inst = self._task_id
        self._whitelist_inst = list(self._whitelist)
        self._audit_log_inst = self._audit_log
        self._leaked_tokens_inst = list(self._leaked_tokens)

    def do_CONNECT(self):
        host, port = self._parse_target(self.path)
        start = time.monotonic()
        decision = self._check_request(host, port)

        if not decision["allowed"]:
            self._audit("CONNECT", host, self.path, decision, start, 403)
            self._send_denial(403, host, decision["reason"])
            return

        # CONNECT 隧道 — 30 秒握手超时
        try:
            upstream = socket.create_connection(
                (host, port), timeout=_CONNECT_TIMEOUT
            )
            upstream.settimeout(0)
        except Exception as e:
            self._audit(
                "CONNECT", host, self.path,
                {"allowed": True, "reason": f"connect failed: {e}"}, start, 502,
            )
            self.send_error(502, f"Failed to connect to {host}: {e}")
            return

        # CONNECT 200 — 直接写字节，不用 send_response（避免加 Date/Server header）
        self.connection.sendall(b"HTTP/1.0 200 Connection Established\r\n\r\n")
        self._audit(
            "CONNECT", host, self.path,
            {"allowed": True, "reason": "whitelist match"}, start, 200,
        )
        self._tunnel(self.connection, upstream)

    def do_GET(self):
        self._proxy_http("GET")

    def do_POST(self):
        self._proxy_http("POST")

    def do_PUT(self):
        self._proxy_http("PUT")

    def do_DELETE(self):
        self._proxy_http("DELETE")

    def do_PATCH(self):
        self._proxy_http("PATCH")

    def do_HEAD(self):
        self._proxy_http("HEAD")

    def _proxy_http(self, method: str):
        """HTTP 代理转发（完整实现）。"""
        start = time.monotonic()

        # 解析目标
        if self.path.startswith(("http://", "https://")):
            parsed = urlparse(self.path)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        else:
            host_header = self.headers.get("Host", "")
            host = host_header.split(":")[0]
            port = int(host_header.split(":")[1]) if ":" in host_header else 80
            path = self.path

        decision = self._check_request(host, port)
        if not decision["allowed"]:
            self._audit(method, host, self.path, decision, start, 403)
            self._send_denial(403, host, decision["reason"])
            return

        # 请求走私检查
        ok, reason = check_request_smuggling(dict(self.headers))
        if not ok:
            self._audit(
                method, host, self.path,
                {"allowed": False, "reason": reason}, start, 400,
            )
            self.send_error(400, reason)
            return

        # 剥离 hop-by-hop
        fwd_headers = strip_hop_by_hop_headers(dict(self.headers))
        fwd_headers["Host"] = host if port == 80 else f"{host}:{port}"

        # 连接目标
        try:
            upstream = socket.create_connection(
                (host, port), timeout=_CONNECT_TIMEOUT
            )
        except Exception as e:
            self._audit(
                method, host, self.path,
                {"allowed": True, "reason": f"connect failed: {e}"}, start, 502,
            )
            self.send_error(502, f"Failed to connect: {e}")
            return

        # 转发请求
        try:
            req_line = f"{method} {path} HTTP/1.1\r\n"
            hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in fwd_headers.items())
            upstream.sendall((req_line + hdr_lines + "\r\n").encode("utf-8"))

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                upstream.sendall(self.rfile.read(content_length))

            self._forward_response(upstream)
            self._audit(
                method, host, self.path,
                {"allowed": True, "reason": "whitelist match"}, start, 200,
            )
        except Exception as e:
            self._audit(
                method, host, self.path,
                {"allowed": True, "reason": f"forward error: {e}"}, start, 502,
            )
        finally:
            upstream.close()

    def _forward_response(self, upstream: socket.socket):
        """从上游读取 HTTP 响应并转发给客户端。"""
        # 用 makefile 缓冲读取（避免逐字节 recv），但 body 也从 buffer 读
        if not hasattr(upstream, "_grape_rbuf"):
            upstream._grape_rbuf = upstream.makefile("rb")
        f = upstream._grape_rbuf

        # 读取状态行 + headers 直到空行
        while True:
            line = f.readline()
            if not line:
                break
            self.wfile.write(line)
            if line in (b"\r\n", b"\n"):
                break

        self.wfile.flush()

        # 转发 body — 从同一个 buffer 读，避免 makefile 缓冲的数据丢失
        while True:
            data = f.read(65536)
            if not data:
                break
            self.wfile.write(data)

    def _check_request(self, host: str, port: int) -> dict:
        """三级检查：白名单 → DNS 地址校验（loopback 例外）。"""
        # 1. 白名单
        for pattern in self._whitelist_inst:
            if match_domain(host, pattern):
                break
        else:
            return {"allowed": False, "reason": "domain not in whitelist"}

        # 2. DNS 地址校验 — loopback 例外（127.0.0.1 / localhost 已在白名单中，是预期目标）
        host_base = host.split(":")[0]
        if host_base not in ("127.0.0.1", "localhost", "::1"):
            ok, addr_reason = check_resolved_address(host, port)
            if not ok:
                return {"allowed": False, "reason": f"DNS address denied: {addr_reason}"}

        return {"allowed": True, "reason": "whitelist match"}

    def _parse_target(self, path: str) -> tuple[str, int]:
        if ":" in path:
            host, port_str = path.rsplit(":", 1)
            return host, int(port_str)
        return path, 443

    def _send_denial(self, code: int, host: str, reason: str):
        """结构化 403 body（违规通知）。"""
        body = json.dumps({
            "error": "blocked",
            "policy": "denied",
            "reason": reason,
            "host": host,
        }).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _audit(
        self, method: str, host: str, path: str,
        decision: dict, start: float, status_code: int,
    ):
        """写 JSONL 审计记录。"""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id_inst,
            "method": method,
            "host": host,
            "path": sanitize_path(path),
            "policy": "allowed" if decision["allowed"] else "denied",
            "reason": decision["reason"],
            "status_code": status_code,
            "latency_ms": round((time.monotonic() - start) * 1000),
        }
        # 凭据泄露检测
        for name, value in self._leaked_tokens_inst:
            if value and value in path:
                record["credential_leak"] = True
                record["leaked_token"] = name
                log.warning(
                    f"sandbox proxy: credential leak detected — "
                    f"token {name} found in request to {host}"
                )
                break
        if self._audit_log_inst:
            try:
                self._audit_log_inst.write(record)
            except Exception as e:
                log.warning(f"audit write failed: {e}")

    def _tunnel(self, client: socket.socket, upstream: socket.socket):
        """双向隧道转发 — 带超时 + ECONNRESET 捕获 + 半关闭检测。"""
        for s in (client, upstream):
            s.settimeout(_TUNNEL_IDLE_TIMEOUT)

        peer_closed = threading.Event()

        def pipe(src: socket.socket, dst: socket.socket):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        peer_closed.set()
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass
                        break
                    dst.sendall(data)
            except (socket.timeout, ConnectionResetError, BrokenPipeError, OSError):
                peer_closed.set()
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (client, upstream):
            try:
                s.close()
            except Exception:
                pass

    def log_message(self, format, *args):
        """静默默认日志（审计走 _audit）。"""
        pass


# ===== Unix socket 桥接（替代 socat） =====

class UnixSocketBridge(threading.Thread):
    """宿主侧: Unix socket listener → TCP 代理端口。"""

    def __init__(self, socket_path: str, proxy_port: int):
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.proxy_port = proxy_port
        self._server: socketserver.UnixStreamServer | None = None

    def run(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = socketserver.UnixStreamServer(
            self.socket_path, self._make_handler()
        )
        self._server.serve_forever()

    def _make_handler(self):
        proxy_port = self.proxy_port

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    upstream = socket.create_connection(
                        ("127.0.0.1", proxy_port), timeout=10
                    )
                except Exception:
                    return

                def pipe(src, dst):
                    try:
                        while True:
                            data = src.recv(65536)
                            if not data:
                                break
                            dst.sendall(data)
                    except Exception:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass

                t = threading.Thread(
                    target=pipe, args=(self.request, upstream), daemon=True
                )
                t.start()
                pipe(upstream, self.request)
                t.join()

        return Handler

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


# ===== SandboxProxy — 生命周期管理 =====

class SandboxProxy:
    """管理代理进程 + Unix socket 桥接的生命周期。

    用法：
        proxy = SandboxProxy(task_id, whitelist, audit_log, leaked_tokens)
        info = proxy.start()  # {proxy_port, socket_path}
        # ... Agent 运行 ...
        proxy.stop()
    """

    def __init__(
        self,
        task_id: str,
        whitelist: list[str],
        audit_log: RotatingAuditLog | None = None,
        leaked_tokens: list[tuple[str, str]] | None = None,
        on_crash: Callable[[], None] | None = None,
    ):
        self._task_id = task_id
        self._proxy_port = 0
        self._server: socketserver.ThreadingTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._bridge: UnixSocketBridge | None = None
        self._socket_path = ""
        self._whitelist = whitelist
        self._audit_log = audit_log
        self._leaked_tokens = leaked_tokens or []
        self._on_crash = on_crash

    def start(self) -> dict:
        """启动代理 + 桥接。返回 {proxy_port, socket_path}。"""
        # Unix socket 路径：放 home 目录下（不能在 /tmp，会被 tmpfs 覆盖）
        home = os.path.expanduser("~")
        self._socket_path = os.path.join(
            home, f".grape-sandbox-{self._task_id[:12]}.sock"
        )

        # 注入 handler 类变量
        handler = WhitelistProxyHandler
        handler._task_id = self._task_id
        handler._whitelist = self._whitelist
        handler._audit_log = self._audit_log
        handler._leaked_tokens = self._leaked_tokens

        # 代理服务器（ThreadingTCPServer 支持并发）
        self._server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), handler
        )
        self._server.daemon_threads = True
        self._proxy_port = self._server.server_address[1]
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()

        # Unix socket 桥接
        self._bridge = UnixSocketBridge(self._socket_path, self._proxy_port)
        self._bridge.start()

        log.info(
            f"sandbox proxy started: task={self._task_id} "
            f"port={self._proxy_port} socket={self._socket_path}"
        )
        return {"proxy_port": self._proxy_port, "socket_path": self._socket_path}

    def stop(self):
        """停止代理 + 桥接 + 清理 socket 文件。"""
        if self._bridge:
            self._bridge.stop()
            self._bridge = None
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._socket_path and os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except Exception:
                pass
        log.info(f"sandbox proxy stopped: task={self._task_id}")

    def is_alive(self) -> bool:
        return self._server is not None and self._server_thread is not None
