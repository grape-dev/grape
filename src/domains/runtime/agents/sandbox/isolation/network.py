"""网络隔离组件——HTTP CONNECT 代理 + Unix socket 桥接。

从 proxy.py 迁移，封装成 NetworkProxy 对象实现 NetworkIsolation Protocol。

--unshare-net 后沙箱内只有 loopback，代理不能监听 host TCP。
方案：host 侧 Unix socket → bwrap --bind 挂进沙箱 → 沙箱内 TCP→Unix bridge。
但当前实现中 UnixSocketBridge 是 host 侧 Unix socket → TCP 代理端口（供非沙箱场景用）。
沙箱场景下 bwrap --bind 挂载 Unix socket 进沙箱，沙箱内 bridge 连接 Unix socket。

设计参考：anthropics/sandbox-runtime（Unix socket 桥接 + domain-pattern + resolved-address-guard）

Skill egress Phase 1+2（design-skill-egress-domains.md）：
- NetworkProxy 接受 EgressPolicy 策略对象（Layer 2 审批后）
- _check_request 拆为 4 函数：_check_deny_lists / _check_admin_whitelist / _check_skill_egress / _check_l7
- 拒绝列表分级：绝对拒绝（永不可覆盖）+ 默认拒绝（Phase 1+2 无管理员例外）
- IPv4-mapped IPv6 归一化（::ffff:127.0.0.1 → 127.0.0.1）
- HTTP L7 校验：methods / path_prefix / max_body / redirect（仅 HTTP 标准转发）
- HTTPS SNI vs CONNECT host 一致性校验
- 审计增强：source / skill_id / resolved_ip / bytes
"""
from __future__ import annotations

import http.server
import ipaddress
import logging
import os
import re
import socket
import socketserver
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

log = logging.getLogger("devops-bot")


# ===== NetworkIsolation Protocol =====

class NetworkIsolation(Protocol):
    """网络隔离组件——与 Sandbox 同生共死。

    start() 在 __enter__ 时调用，stop() 在 cleanup() 中调用。
    env() 返回的代理变量由 wrap() 烘进 bwrap --setenv。

    --unshare-net 后沙箱内只有 loopback，代理不能监听 host TCP。
    方案：host 侧 Unix socket → bwrap --bind 挂进沙箱 → 沙箱内 TCP→Unix bridge。
    """

    def start(self, task_id: str) -> None:
        """启动代理 + Unix socket + bridge。失败抛 NetworkStartupError。"""
        ...

    def stop(self) -> None:
        """关闭。幂等。"""
        ...

    def env(self) -> dict[str, str]:
        """返回代理环境变量。"""
        ...

    def is_running(self) -> bool: ...

    def socket_mount_path(self) -> str:
        """Unix socket host 侧路径。"""
        ...

# ===== 常量 =====

_CONNECT_TIMEOUT = 30
_TUNNEL_IDLE_TIMEOUT = 300

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade",
})

_PROXY_ENV_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "GRPC_PROXY", "grpc_proxy",
    "BUN_HTTP_PROXY",  # Bun 引擎专用（opencode 用 Bun，不读标准 HTTP_PROXY）
]

_NO_PROXY_VALUES = "127.0.0.1,localhost,::1"

# 回环 host 集合——用于等价性判定（不归一化连接目标）
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# ===== 拒绝列表分级（skill egress Phase 1+2）=====

# 绝对拒绝——永不可覆盖
_ABSOLUTE_DENIED = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),   # 含云元数据 169.254.169.254
    ipaddress.ip_network("fe80::/10"),         # link-local——绝对拒绝，无合法出站理由
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),       # 组播
    ipaddress.ip_network("255.255.255.255/32"),
]

# 默认拒绝——Phase 1+2 无管理员例外，命中即拒绝
# Phase 3: 查询 admin_exceptions (scope/ttl/审批记录)
_DEFAULT_DENIED = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),       # IPv6 ULA
]

_CLOUD_METADATA_ADDRESSES = [
    "100.100.100.200",
    "168.63.129.16",
    "192.0.0.192",
]

_SENSITIVE_QUERY = re.compile(
    r"(token|key|secret|password|access_token|private_token|ak|sk)=([^&]+)",
    re.IGNORECASE,
)

# SNI 窥探上限
_MAX_CLIENTHELLO_SIZE = 16384  # 16KB

# _read_status_line 限制
_MAX_STATUS_LINE = 1024      # 状态行 1KB
_MAX_RESPONSE_HEADERS = 8192  # 响应头整体 8KB


def _is_loopback_host(host: str) -> bool:
    """判断 host 是否为回环地址（127.0.0.0/8 全段 + ::1 + localhost）。"""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _remove_from_csv(csv_str: str, value: str) -> str:
    """从逗号分隔字符串中安全删除一个值（不破坏其他值）。"""
    parts = [p.strip() for p in csv_str.split(",")]
    parts = [p for p in parts if p and p != value]
    return ",".join(parts)


# ===== IP 归一化 + 拒绝列表校验 =====

def _normalize_ip(ip_str: str) -> ipaddress._BaseAddress:
    """归一化 IP——IPv4-mapped IPv6 转 IPv4 统一匹配。

    用 ipaddress.ip_address(x).ipv4_mapped or ip，不自写位运算。
    归一化后再查拒绝列表，顺序不能反。
    """
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped  # ::ffff:127.0.0.1 → 127.0.0.1
    return ip


def _check_ip_deny_lists(ip: ipaddress._BaseAddress) -> tuple[bool, str]:
    """IP 过拒绝列表。返回 (ok, reason)。

    先查绝对拒绝，再查默认拒绝。
    Phase 1+2: 默认拒绝无例外，命中即拒绝。
    Phase 3: 查询 admin_exceptions (scope/ttl/审批记录)。
    """
    for network in _ABSOLUTE_DENIED:
        if ip in network:
            return False, f"address in absolute deny list {network}"
    for network in _DEFAULT_DENIED:
        if ip in network:
            return False, f"address in default deny list {network} (internal network)"
    for addr in _CLOUD_METADATA_ADDRESSES:
        if str(ip) == addr:
            return False, "cloud metadata address"
    return True, "ok"


# ===== 域名匹配 =====

def match_domain(host: str, pattern: str) -> bool:
    """白名单域名匹配——支持通配符。DNS 大小写不敏感，统一转小写。"""
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if "." not in suffix or suffix.startswith(".") or suffix.endswith("."):
            return False
        host_base = host.split(":")[0]
        return host_base.endswith("." + suffix)
    host_base = host.split(":")[0]
    pattern_base = pattern.split(":")[0]
    return host_base == pattern_base


# ===== DNS 地址校验 =====

def check_resolved_address(host: str, port: int) -> tuple[bool, str, str]:
    """DNS 解析后校验 IP——防止 DNS 重绑定到内网/元数据接口。

    返回 (ok, reason, resolved_ip)。
    resolved_ip 是解析后的 IP 地址，调用方应直接用此 IP 连接，
    避免 create_connection 再次 DNS 解析（TOCTOU 重绑定）。
    """
    try:
        ip = _normalize_ip(host)
        ok, reason = _check_ip_deny_lists(ip)
        return ok, reason, str(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}", ""
    resolved_ip = ""
    for family, _, _, _, sockaddr in infos:
        ip_str = str(sockaddr[0])
        try:
            ip = _normalize_ip(ip_str)
        except ValueError:
            continue
        ok, reason = _check_ip_deny_lists(ip)
        if not ok:
            return False, f"resolved to {ip_str}: {reason}", ""
        if not resolved_ip:
            resolved_ip = ip_str  # 第一个通过校验的 IP
    if not resolved_ip:
        return False, "no valid IP resolved", ""
    return True, "ok", resolved_ip


# ===== HTTP 请求处理 =====

def strip_hop_by_hop_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def check_request_smuggling(headers: dict) -> tuple[bool, str]:
    has_cl = any(k.lower() == "content-length" for k in headers)
    has_te = any(k.lower() == "transfer-encoding" for k in headers)
    if has_cl and has_te:
        return False, "Content-Length and Transfer-Encoding both present (smuggling risk)"
    return True, "ok"


def sanitize_path(path: str) -> str:
    def _redact(m: re.Match) -> str:
        return f"{m.group(1)}=[REDACTED]"
    return _SENSITIVE_QUERY.sub(_redact, path)


# ===== SNI 提取 =====

def _extract_sni(client_socket: socket.socket) -> tuple[str | None, bytes]:
    """从 TLS ClientHello 提取 SNI。

    返回 (sni_hostname, buffered_bytes)。
    sni_hostname: SNI 域名，或 None（读不到/ECH/分片超限/非 TLS）。
    buffered_bytes: 已读但未消费的字节（ClientHello + 可能的后续数据），
    调用方必须将这些 bytes 先发给 upstream 再启动隧道。

    None = fail-closed 拒绝。

    约束：
    - 拼接 ClientHello 上限 16KB，超限 fail-closed 断连 + 审计
    - 处理 TLS record 分片、Handshake 分片、ClientHello 分片三层
    - 只读第一个 ClientHello，重协商不再校验
    - 非 TLS 流量伪装 CONNECT：读不到 ClientHello 就断连
    """
    try:
        buf = b""
        # 1. 读 TLS record header（5 bytes: type=0x16, version 2B, length 2B）
        while len(buf) < 5:
            chunk = client_socket.recv(5 - len(buf))
            if not chunk:
                return None, b""
            buf += chunk

        if buf[0] != 0x16:  # 不是 Handshake type
            return None, buf  # 非 TLS——断连，不检查是否"像 TLS"

        # 读第一个 TLS record 的长度
        record_len = struct.unpack("!H", buf[3:5])[0]
        if record_len == 0:
            return None, b""

        # 2. 拼接完整 TLS record（可能跨多个 recv）
        while len(buf) < 5 + record_len and len(buf) < _MAX_CLIENTHELLO_SIZE:
            need = min(5 + record_len - len(buf), 65536)
            chunk = client_socket.recv(need)
            if not chunk:
                return None, b""
            buf += chunk

        if len(buf) >= _MAX_CLIENTHELLO_SIZE:
            return None, buf  # 超限

        # 可能需要读更多 record（如果 ClientHello 跨多个 TLS record）
        # 检查 Handshake layer 是否完整
        record_data = buf[5:5 + record_len]

        # 3. 解析 Handshake layer
        if len(record_data) < 4:
            return None, b""
        hs_type = record_data[0]
        if hs_type != 0x01:  # ClientHello
            return None, b""
        hs_len = struct.unpack("!I", b"\x00" + record_data[1:4])[0]

        # 如果 Handshake 不完整，继续读更多 TLS record
        while len(record_data) < 4 + hs_len and len(buf) < _MAX_CLIENTHELLO_SIZE:
            # 读下一个 TLS record header
            if len(buf) >= 5 + record_len + 5:
                next_record_len = struct.unpack("!H", buf[5 + record_len + 3:5 + record_len + 5])[0]
                need = min(5 + record_len + 5 + next_record_len - len(buf), 65536)
            else:
                need = 5 + record_len + 5 - len(buf)
            if need <= 0:
                break
            chunk = client_socket.recv(min(need, 65536))
            if not chunk:
                return None, b""
            buf += chunk

        if len(buf) >= _MAX_CLIENTHELLO_SIZE:
            return None, buf  # 超限

        # 4. 解析 ClientHello
        hello = record_data[4:4 + hs_len]
        if len(hello) < 38:
            return None, b""

        # client_version: 2 bytes (offset 0)
        # random: 32 bytes (offset 2)
        # session_id: 1 byte length + data
        offset = 34  # 2 + 32
        session_id_len = hello[offset]
        offset += 1 + session_id_len

        # cipher_suites: 2 bytes length + data
        if offset + 2 > len(hello):
            return None, b""
        cs_len = struct.unpack("!H", hello[offset:offset + 2])[0]
        offset += 2 + cs_len

        # compression_methods: 1 byte length + data
        if offset + 1 > len(hello):
            return None, b""
        cm_len = hello[offset]
        offset += 1 + cm_len

        # extensions: 2 bytes total length
        if offset + 2 > len(hello):
            return None, buf  # 无 extensions
        ext_total = struct.unpack("!H", hello[offset:offset + 2])[0]
        offset += 2

        # 遍历 extensions 找 SNI (type=0x0000)
        ext_end = offset + ext_total
        while offset + 4 <= ext_end and offset + 4 <= len(hello):
            ext_type = struct.unpack("!H", hello[offset:offset + 2])[0]
            ext_len = struct.unpack("!H", hello[offset + 2:offset + 4])[0]
            offset += 4
            if offset + ext_len > len(hello):
                break
            if ext_type == 0x0000:  # SNI extension
                # ServerNameList: 2 bytes length, then entries
                ext_data = hello[offset:offset + ext_len]
                if len(ext_data) < 2:
                    break
                snl_len = struct.unpack("!H", ext_data[0:2])[0]
                snl_offset = 2
                while snl_offset + 3 <= len(ext_data) and snl_offset < 2 + snl_len:
                    name_type = ext_data[snl_offset]
                    name_len = struct.unpack("!H", ext_data[snl_offset + 1:snl_offset + 3])[0]
                    snl_offset += 3
                    if name_type == 0 and snl_offset + name_len <= len(ext_data):
                        # host_name
                        return ext_data[snl_offset:snl_offset + name_len].decode("ascii", errors="replace"), buf
                    snl_offset += name_len
                break
            offset += ext_len

        return None, buf  # 无 SNI
    except Exception:
        return None, b""


# ===== 代理 Handler =====

class _WhitelistProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 正向代理：CONNECT（HTTPS 隧道）+ HTTP 转发。

    Skill egress Phase 1+2：支持 EgressPolicy 策略对象校验。
    """

    _task_id: str = ""
    _whitelist: list[str] = []
    _audit_log = None
    _leaked_tokens: list[tuple[str, str]] = []
    # local_gateway 端口级白名单——仅声明的端口允许访问 127.0.0.1
    _local_gateway_ports: set[int] = set()
    # skill egress 策略（per-task，Layer 2 审批后）
    _egress_policy: Any = None
    # 速率限制器（per-task）
    _rate_limiter: Any = None

    def setup(self):
        """每个请求复制类变量到实例——线程安全（静态审查确认，无并发测试覆盖）。

        ThreadingTCPServer 并发处理时多个线程共享 handler 子类类变量，
        setup() 复制到实例变量确保每个请求独立。
        P2 如需更强保障可加并发测试。
        """
        super().setup()
        self._task_id_inst = self._task_id
        self._whitelist_inst = list(self._whitelist)
        self._audit_log_inst = self._audit_log
        self._local_gateway_ports_inst = set(self._local_gateway_ports)
        self._leaked_tokens_inst = list(self._leaked_tokens)
        self._egress_policy_inst = self._egress_policy
        self._rate_limiter_inst = self._rate_limiter

    def do_CONNECT(self):
        host, port = self._parse_target(self.path)
        start = time.monotonic()
        decision = self._check_request(host, port, is_https=True)

        if not decision["allowed"]:
            self._audit("CONNECT", host, self.path, decision, start, 403)
            self._send_denial(403, host, decision["reason"])
            return

        try:
            # P0-8 修复：用 DNS 校验时解析的 IP 连接，避免 TOCTOU 重绑定
            _connect_host = decision.get("resolved_ip", host)
            upstream = socket.create_connection((_connect_host, port), timeout=_CONNECT_TIMEOUT)
            upstream.settimeout(None)  # 阻塞模式——tunnel 需要阻塞 recv
        except Exception as e:
            self._audit("CONNECT", host, self.path,
                        {"allowed": True, "reason": f"connect failed: {e}"}, start, 502)
            self.send_error(502, f"Failed to connect to {host}: {e}")
            return

        self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # SNI 校验——读 ClientHello，提取 SNI，校验与 CONNECT host 一致
        # 只对 skill egress 来源的请求做（admin 白名单来源已信任，跳过 SNI 校验）
        sni_buffered = b""
        if decision.get("source", "").startswith("skill"):
            sni, sni_buffered = _extract_sni(self.connection)
            if sni is not None:
                if sni.lower() != host.lower():
                    upstream.close()
                    sni_decision = {"allowed": False,
                                    "reason": f"SNI mismatch: {sni} != {host}",
                                    "source": "skill"}
                    self._audit("CONNECT", host, self.path, sni_decision, start, 403)
                    self._send_denial(403, host, sni_decision["reason"])
                    self.close_connection = True
                    return
            else:
                # SNI 不可见（ECH / 非 TLS / 分片超限）→ fail-closed
                upstream.close()
                sni_decision = {"allowed": False,
                                "reason": "SNI not visible (ECH or non-TLS or fragmented)",
                                "source": "skill"}
                self._audit("CONNECT", host, self.path, sni_decision, start, 403)
                self._send_denial(403, host, sni_decision["reason"])
                self.close_connection = True
                return
            # SNI 校验通过——把已读的 ClientHello bytes 先发给 upstream
            if sni_buffered:
                upstream.sendall(sni_buffered)

        self._audit("CONNECT", host, self.path,
                    {"allowed": True, "reason": "whitelist match",
                     "source": decision.get("source", "")}, start, 200)
        self._tunnel(self.connection, upstream)
        self.close_connection = True

    def do_GET(self): self._proxy_http("GET")
    def do_POST(self): self._proxy_http("POST")
    def do_PUT(self): self._proxy_http("PUT")
    def do_DELETE(self): self._proxy_http("DELETE")
    def do_PATCH(self): self._proxy_http("PATCH")
    def do_HEAD(self): self._proxy_http("HEAD")

    def _proxy_http(self, method: str):
        host, port = self._parse_target_from_headers()
        if not host:
            self.send_error(400, "Missing Host header")
            return
        start = time.monotonic()
        decision = self._check_request(host, port or 80, method=method, is_https=False)

        if not decision["allowed"]:
            self._audit(method, host, self.path, decision, start, 403)
            self._send_denial(403, host, decision["reason"])
            return

        try:
            # P0-8 修复：用 DNS 校验时解析的 IP 连接，避免 TOCTOU 重绑定
            _connect_host = decision.get("resolved_ip", host)
            upstream = socket.create_connection((_connect_host, port or 80), timeout=_CONNECT_TIMEOUT)
        except Exception as e:
            self._audit(method, host, self.path,
                        {"allowed": True, "reason": f"connect failed: {e}"}, start, 502)
            self.send_error(502, f"Failed to connect: {e}")
            return

        # 转发请求行 + headers
        # HTTP 代理请求行是完整 URL（POST http://host:port/path HTTP/1.1）
        # upstream 是原始服务器，需要转成路径（POST /path HTTP/1.1）
        path = self.path
        if path.startswith("http://") or path.startswith("https://"):
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(path)
            path = _parsed.path or "/"
            if _parsed.query:
                path += "?" + _parsed.query
        req_line = f"{method} {path} HTTP/1.1\r\n"
        headers = strip_hop_by_hop_headers(dict(self.headers))
        ok, reason = check_request_smuggling(headers)
        if not ok:
            self.send_error(400, reason)
            return
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        upstream.sendall((req_line + header_lines + "\r\n").encode())

        # 转发 body（max_body 流式计数——仅 skill egress 来源做）
        rule = decision.get("rule")
        if rule:
            # skill egress 来源——做 max_body 校验
            cl = int(self.headers.get("Content-Length", 0))
            if cl > 0:
                max_body = rule.max_body
                transferred = 0
                remaining = cl
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > max_body:
                        upstream.close()
                        body_decision = {"allowed": False,
                                         "reason": f"max_body exceeded {max_body}",
                                         "source": "skill"}
                        self._audit(method, host, path, body_decision, start, 413)
                        self._send_denial(413, host, body_decision["reason"])
                        return
                    upstream.sendall(chunk)
                    remaining -= len(chunk)
        else:
            # admin 白名单来源——BUG-16 修复：加 max_body 校验（10MB 上限）
            _ADMIN_MAX_BODY = 10 * 1024 * 1024
            cl = int(self.headers.get("Content-Length", 0))
            if cl > _ADMIN_MAX_BODY:
                self._audit(method, host, path,
                            {"allowed": False, "reason": f"admin max_body exceeded {_ADMIN_MAX_BODY}",
                             "source": "admin"}, 0, 413)
                self._send_denial(413, host, f"admin max_body exceeded {_ADMIN_MAX_BODY}")
                return
            if cl > 0:
                upstream.sendall(self.rfile.read(cl))

        # 转发响应
        # redirect 校验——仅 skill egress 来源且 redirect=none 时做
        if rule and rule.redirect == "none":
            status_code, buffered = self._read_status_line(upstream)
            if status_code == -1:
                upstream.close()
                err_decision = {"allowed": False,
                                "reason": "failed to read upstream status line",
                                "source": "skill"}
                self._audit(method, host, path, err_decision, start, 502)
                self.send_error(502, "Failed to read upstream response")
                return
            if 300 <= status_code < 400:
                upstream.close()
                redir_decision = {"allowed": False,
                                  "reason": f"redirect denied (status {status_code})",
                                  "source": "skill"}
                self._audit(method, host, path, redir_decision, start, 403)
                self._send_denial(403, host, redir_decision["reason"])
                return
            # 非重定向——先发 buffered 字节给 client，再启动 _tunnel
            if buffered:
                self.connection.sendall(buffered)
            self._tunnel(upstream, self.connection)
        else:
            # redirect 允许或 admin 来源——直接 _tunnel 字节流（现有行为不变）
            self._tunnel(upstream, self.connection)

        self._audit(method, host, path,
                    {"allowed": True, "reason": "whitelist match",
                     "source": decision.get("source", "")}, start, 200)
        self.close_connection = True

    def _read_status_line(self, upstream: socket.socket) -> tuple[int, bytes]:
        """从 upstream 读 HTTP 响应状态行。

        只读到第一个 \\r\\n（状态行），返回 (status_code, buffered_bytes)。
        buffered_bytes 是已读但未消费的字节（状态行 + 可能的部分 headers），
        需要在后续 _tunnel 中先发给 client。

        状态行和响应头是两个不同限制，不混：
        - 状态行：行级限制 1KB，超限断连 + 审计
        - 响应头整体：8KB 限制，超限断连 + 审计
        两者超限都断连 + 审计，不截断后继续。

        不解析 headers/body——保持 _tunnel 字节流行为。
        """
        buf = b""
        upstream.settimeout(10)
        try:
            while b"\r\n" not in buf:
                if len(buf) > _MAX_STATUS_LINE:
                    return -1, buf  # 状态行超限
                chunk = upstream.recv(4096)
                if not chunk:
                    return -1, buf
                buf += chunk
        finally:
            upstream.settimeout(None)

        line_end = buf.index(b"\r\n") + 2
        status_line = buf[:line_end]
        remaining = buf[line_end:]

        try:
            parts = status_line.split(b" ", 2)
            status_code = int(parts[1])
        except (IndexError, ValueError):
            return -1, buf

        return status_code, remaining

    def _parse_target(self, connect_path: str) -> tuple[str, int]:
        """CONNECT host:port → (host, port)。"""
        parts = connect_path.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 443
        return host, port

    def _parse_target_from_headers(self) -> tuple[str, int | None]:
        """HTTP 请求从 Host header 解析目标。"""
        host_header = self.headers.get("Host", "")
        if ":" in host_header:
            host, port_str = host_header.rsplit(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                return host, None
        return host_header, None

    # ===== _check_request 拆 4 函数 =====

    def _check_request(self, host: str, port: int, method: str = "",
                       is_https: bool = False) -> dict:
        """完整决策——6 步逐请求。

        1. 绝对拒绝 + 默认拒绝（所有来源都必须过，admin 白名单也不例外）
        2. admin 白名单（per-task：引擎 LLM API + grape host + DB 全局 + ConfigSync）
           命中后跳过 skill egress 和 L7 校验，审计标 source=admin:*
        3. skill egress 匹配
        4. L7 校验（HTTP only）
        """
        # 1. 拒绝列表（绝对拒绝 + 默认拒绝）——只做拒绝检查，不做白名单检查
        deny = self._check_deny_lists(host, port)
        if not deny["allowed"]:
            return deny
        # resolved_ip 从 DNS 校验中获取（可能用于后续连接）
        resolved_ip = deny.get("resolved_ip", "")

        # 2. admin 白名单（per-task）
        admin = self._check_admin_whitelist(host, port)
        if admin["allowed"]:
            if resolved_ip:
                admin["resolved_ip"] = resolved_ip
            return admin  # source = "admin:engine" / "admin:grape" / "admin:db"

        # 3. skill egress
        skill = self._check_skill_egress(host, port)
        if not skill["allowed"]:
            return skill  # source = "skill", denied
        if resolved_ip:
            skill["resolved_ip"] = resolved_ip

        # 4. L7 校验（HTTP only）
        if not is_https and method:
            l7 = self._check_l7(skill["rule"], method, getattr(self, "path", "/"))
            if not l7["allowed"]:
                return l7

        # 5. 速率限制（Phase 3）
        if self._rate_limiter_inst:
            ok, reason = self._rate_limiter_inst.check_request(host)
            if not ok:
                return {"allowed": False, "reason": reason, "source": "skill"}

        return skill  # source = "skill", allowed

    def _check_deny_lists(self, host: str, port: int) -> dict:
        """步骤 1：绝对拒绝 + 默认拒绝。

        只做拒绝列表检查，不做白名单检查。
        通过拒绝列表 → allowed=True（后续步骤检查白名单）。
        命中拒绝列表 → allowed=False。
        localhost → 绝对拒绝（除 local_gateway 端口例外）。
        IPv4-mapped IPv6 → 归一化后检查，防止 ::ffff:127.0.0.1 绕过。
        """
        # IPv4-mapped IPv6 归一化——防止 ::ffff:127.0.0.1 绕过 loopback 检查
        # _is_loopback_host 对 ::ffff:127.0.0.1 返回 True，但 local_gateway 例外
        # 不应覆盖 IPv4-mapped 地址——归一化后按 IPv4 处理
        normalized_host = host
        try:
            ip = _normalize_ip(host)
            if isinstance(ip, ipaddress.IPv4Address) and str(ip) != host:
                # host 是 IPv4-mapped IPv6（如 ::ffff:127.0.0.1）→ 归一化为 IPv4
                normalized_host = str(ip)
        except ValueError:
            pass

        # localhost / loopback——绝对拒绝，除 local_gateway 声明的端口
        if _is_loopback_host(normalized_host):
            if self._local_gateway_ports_inst and port in self._local_gateway_ports_inst:
                # local_gateway 例外——允许声明的端口访问回环
                # 但 IPv4-mapped 地址不享受此例外
                if normalized_host == host:
                    return {"allowed": True, "reason": "local_gateway port match",
                            "source": "admin:engine", "resolved_ip": "127.0.0.1"}
                # IPv4-mapped 地址——绝对拒绝，不享受 local_gateway 例外
                return {"allowed": False,
                        "reason": f"IPv4-mapped {host} -> {normalized_host} in absolute deny list"}
            return {"allowed": False, "reason": f"loopback address {normalized_host} in absolute deny list"}

        # IP 地址——归一化后查拒绝列表
        try:
            ip = _normalize_ip(host)
            ok, reason = _check_ip_deny_lists(ip)
            if not ok:
                return {"allowed": False, "reason": reason}
            # 通过拒绝列表——不在此处做白名单检查，交给后续步骤
            return {"allowed": True, "reason": "passed deny lists",
                    "resolved_ip": str(ip)}
        except ValueError:
            pass  # 不是 IP，是域名

        # 域名——做 DNS 解析 + IP 校验
        ok, reason, resolved_ip = check_resolved_address(host, port)
        if not ok:
            return {"allowed": False, "reason": f"DNS guard: {reason}"}

        # DNS 校验通过——返回 resolved_ip，allowed=True 交给后续步骤
        return {"allowed": True, "reason": "passed deny lists",
                "resolved_ip": resolved_ip}

    def _check_admin_whitelist(self, host: str, port: int) -> dict:
        """步骤 2：admin 白名单（per-task）。

        来源：引擎 LLM API 域名 + grape host + DB 全局 + ConfigSync。
        这些来源 skill 无法影响——per-task 最小化不受影响。
        命中后跳过 skill egress 和 L7 校验，审计标 source=admin:*。

        注意：此处不再做 DNS/IP 校验——_check_deny_lists 已做过。
        resolved_ip 从 _check_deny_lists 的返回值传递。
        """
        matched = False
        source = "admin:db"  # 默认来源

        for pattern in self._whitelist_inst:
            if match_domain(host, pattern):
                matched = True
                break
        if not matched:
            return {"allowed": False, "reason": "not in admin whitelist"}

        # 判断来源——用于审计细分
        # grape host
        server_url = os.environ.get("GRAPE_SERVER_URL", "") or os.environ.get("RUNTIME_SERVER_URL", "")
        if server_url:
            _grape_host = urlparse(server_url).hostname or ""
            if _grape_host and match_domain(host, _grape_host):
                source = "admin:grape"

        # 引擎 LLM API 域名——在 NetworkProxy.start() 合并到 whitelist 时不标记来源
        # 这里简化：非 grape 的管理员白名单域名标 admin:engine
        if source == "admin:db" and not _is_loopback_host(host):
            source = "admin:engine"

        # local_gateway 端口已在 _check_deny_lists 中处理

        return {"allowed": True, "reason": "admin whitelist match",
                "source": source}

    def _check_skill_egress(self, host: str, port: int) -> dict:
        """步骤 3：skill egress 策略匹配。

        遍历 EgressPolicy.rules，匹配 host + port。
        返回匹配的 EgressRule（供 L7 校验用）。
        """
        if not self._egress_policy_inst:
            return {"allowed": False, "reason": "no skill egress policy",
                    "source": "skill"}

        policy = self._egress_policy_inst
        for rule in policy.rules:
            # host 匹配
            if rule.type == "domain":
                # 域名精确匹配（非通配符）
                if host.lower() != rule.host.lower():
                    continue
            elif rule.type == "ip":
                # IP 精确匹配
                try:
                    rule_ip = _normalize_ip(rule.host)
                    target_ip = _normalize_ip(host)
                    if rule_ip != target_ip:
                        continue
                except ValueError:
                    continue
            else:
                continue

            # 端口校验
            if port not in rule.ports:
                continue

            # 匹配成功
            return {"allowed": True, "reason": "skill egress match",
                    "source": "skill", "rule": rule,
                    "skill_id": self._egress_policy_inst.skill_id,
                    "skill_digest": self._egress_policy_inst.skill_digest}

        return {"allowed": False, "reason": f"host '{host}:{port}' not in skill egress rules",
                "source": "skill",
                "skill_id": getattr(self._egress_policy_inst, "skill_id", "unknown"),
                "skill_digest": getattr(self._egress_policy_inst, "skill_digest", "")}

    def _check_l7(self, rule, method: str, path: str) -> dict:
        """步骤 4：L7 校验（仅 HTTP 标准转发）。

        CONNECT 隧道不调用此函数。
        """
        from ..isolation.policy import check_method, check_path_prefix

        # 方法校验
        if not check_method(rule, method):
            return {"allowed": False, "reason": f"method '{method}' not allowed",
                    "source": "skill"}

        # path_prefix 校验
        if not check_path_prefix(rule.path_prefix, path):
            return {"allowed": False, "reason": f"path '{path}' not in prefix '{rule.path_prefix}'",
                    "source": "skill"}

        return {"allowed": True, "reason": "L7 check passed",
                "source": "skill", "rule": rule}

    def _tunnel(self, client: socket.socket, upstream: socket.socket):
        """双向管道。Phase 3：加字节计数（速率限制）。"""
        rate_limiter = self._rate_limiter_inst

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    # Phase 3：出站字节计数
                    if rate_limiter:
                        ok, reason = rate_limiter.check_bytes(len(data))
                        if not ok:
                            log.warning(f"rate_limiter byte limit: {reason}")
                            break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        t.start()
        pipe(upstream, client)
        t.join()

    def _send_denial(self, code: int, host: str, reason: str):
        body = f'{{"error": "denied", "host": "{host}", "reason": "{reason}"}}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def _audit(self, method: str, host: str, path: str, decision: dict,
               start: float, status: int):
        if self._audit_log_inst:
            try:
                # 统一用 AuditEvent 对象，兼容 FileAuditLogger.log(AuditEvent)
                from src.domains.runtime.agents.common.audit import AuditEvent, AuditKind, now_iso
                self._audit_log_inst.log(AuditEvent(
                    ts=now_iso(),
                    task_id=self._task_id_inst,
                    kind=AuditKind.NETWORK_DECISION,
                    outcome="allow" if decision.get("allowed") else "deny",
                    host=host,
                    method=method,
                    path=sanitize_path(path),
                    detail={
                        "reason": decision.get("reason", ""),
                        "status": status,
                        "latency_ms": int((time.monotonic() - start) * 1000),
                        "destination_port": self._parse_target(self.path)[1] if method == "CONNECT"
                            else (self._parse_target_from_headers()[1] or 80),
                        # skill egress Phase 1+2 审计增强
                        "source": decision.get("source", ""),
                        "skill_id": decision.get("skill_id", "unknown"),
                        "skill_digest": decision.get("skill_digest", ""),
                        "resolved_ip": decision.get("resolved_ip", ""),
                    },
                ))
            except Exception:
                # Phase 3 fail-closed：审计写入失败 → 断连
                log.error(
                    f"audit write failed for task={self._task_id_inst}, "
                    f"closing connection (fail-closed)")
                try:
                    self.connection.close()
                except Exception:
                    pass

    def log_message(self, format, *args):
        pass  # 静默（审计日志替代）


# ===== Unix socket 桥接 =====

class _UnixSocketBridge(threading.Thread):
    """host 侧 Unix socket listener → TCP 代理端口。

    使用 ThreadingUnixStreamServer 并发处理多个 UDS 连接。
    单线程 UnixStreamServer 会导致并发请求串行排队——
    codearts 同时发起 heartbeat/collector/LLM 等多个 HTTPS 请求时，
    每个请求的 tunnel 在 bridge 层串行执行，导致 10-20s/次的延迟。
    对照实验确认：N=8 并发时单线程 total=4.4s vs 多线程 total=1.4s。
    """

    def __init__(self, socket_path: str, proxy_port: int):
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.proxy_port = proxy_port
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._ready = threading.Event()

    def run(self):
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            self._server = socketserver.ThreadingUnixStreamServer(
                self.socket_path, self._make_handler())
            self._server.daemon_threads = True
            self._ready.set()  # socket 已创建
            self._server.serve_forever()
        except Exception as e:
            log.error(f"UnixSocketBridge failed: {e}")
            self._ready.set()  # 释放等待方

    def _make_handler(self):
        proxy_port = self.proxy_port

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    upstream = socket.create_connection(
                        ("127.0.0.1", proxy_port), timeout=10)
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

                t = threading.Thread(target=pipe, args=(self.request, upstream), daemon=True)
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


# ===== NetworkProxy =====

class _WhitelistCache:
    """全局域名白名单内存缓存。

    异步加载 DB 数据 + 线程锁保护读写。
    多个 NetworkProxy 实例共享同一份缓存，避免每次 start() 都查 DB。
    sandbox_admin API 修改白名单后调 refresh() 刷新缓存。
    """

    _instance: "_WhitelistCache | None" = None
    _lock = threading.Lock()

    def __new__(cls):
        obj = super().__new__(cls)
        obj._domains = set()  # type: ignore[attr-defined]
        obj._loaded = False  # type: ignore[attr-defined]
        obj._load_error = False  # BUG-14: 加载失败标记
        return obj

    @classmethod
    def get(cls) -> "_WhitelistCache":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def load(self) -> set[str]:
        """同步加载（首次调用）。返回当前白名单域名的副本。"""
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._do_load()
        return set(self._domains)

    def refresh(self) -> None:
        """异步刷新（sandbox_admin API 修改后调）。"""
        t = threading.Thread(target=self._refresh_sync, daemon=True)
        t.start()

    def _refresh_sync(self) -> None:
        with self._lock:
            self._do_load()

    def _do_load(self) -> None:
        """从服务端 API 加载白名单域名（不直接查 DB——远程 worker 无 DB 连接）。"""
        try:
            import requests
            server_url = os.environ.get("GRAPE_SERVER_URL", "") or os.environ.get("RUNTIME_SERVER_URL", "")
            token = os.environ.get("RUNTIME_TOKEN", "")
            # 也尝试从 key 文件读 token（路径从环境变量读，不硬编码）
            if server_url and token:
                resp = requests.get(
                    f"{server_url}/api/runtime/whitelist",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    domains = resp.json().get("domains", [])
                    self._domains = set(domains)
                    self._loaded = True
                    log.info(f"WhitelistCache loaded from API: {len(self._domains)} domains")
                    return
            # API 不可用时 fallback 到本地 DB（仅本机 worker 有 DB）
            from src.session_manager import db, db_lock
            with db_lock:
                rows = db.execute(
                    "SELECT DISTINCT domain FROM engine_sandbox_whitelist "
                    "WHERE backend_name='*'"
                ).fetchall()
            self._domains = {r[0] for r in rows if r[0]}
            self._loaded = True
            log.info(f"WhitelistCache loaded from DB fallback: {len(self._domains)} domains")
        except Exception as e:
            log.warning(f"WhitelistCache load failed: {e}")
            if not self._loaded:
                # BUG-14 修复：加载失败时标记 _load_error，但仍设 _loaded=True 防止重试风暴
                # 调用方（NetworkProxy.start）检查 _load_error 并 fail-closed 报错
                # 原代码设 _loaded=True 空白名单但无 _load_error 标记，调用方无法区分"空白名单"和"加载失败"
                self._domains = set()
                self._load_error = True
                self._loaded = True  # 防止重复加载（重试风暴）


class NetworkProxy:
    """网络隔离组件——HTTP CONNECT 代理 + Unix socket 桥接。

    实现 NetworkIsolation Protocol。
    start() 在 __enter__ 时调用，stop() 在 cleanup() 中调用。

    Skill egress Phase 3：
    - egress_policy 参数接受 EgressPolicy 策略对象（Layer 2 审批后）
    - rate_limiter 参数接受 RateLimiter（per-task 速率限制）
    - 审计写入失败 fail-closed 断连
    - 无 egress_policy 时行为不变（向后兼容）
    """

    def __init__(self, whitelist: list[str] | None = None, audit_log=None,
                 local_gateway: str | None = None, egress_policy=None,
                 rate_limiter=None):
        self._whitelist = whitelist or []
        self._audit_log = audit_log
        self._local_gateway = local_gateway  # "127.0.0.1:40001" 或 None
        self._egress_policy = egress_policy  # EgressPolicy | None
        self._rate_limiter = rate_limiter    # RateLimiter | None
        self._proxy_port = 0
        self._server: socketserver.ThreadingTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._bridge: _UnixSocketBridge | None = None
        self._socket_path = ""
        self._task_id = ""

    def start(self, task_id: str) -> None:
        """启动代理 + Unix socket 桥接。失败抛异常。"""
        from ..errors import NetworkStartupError

        self._task_id = task_id
        import hashlib
        # task_id 为空时用 uuid 兜底——不用 "default"（会污染审计/告警聚合）
        if not task_id:
            import uuid as _uuid
            task_id = f"__untracked__:{_uuid.uuid4().hex[:8]}"
            log.warning(f"empty task_id in NetworkProxy, using generated: {task_id}")
        _safe = re.sub(r"[^a-zA-Z0-9_-]", "", task_id)[:16]
        _hash = hashlib.sha256(task_id.encode()).hexdigest()[:8]
        # UDS socket 放在专用目录里——bwrap 0.4.1 不支持 --bind 直接挂载 socket 文件
        # bind-mount 目录可以工作，所以 socket 必须在一个独立目录中
        _uds_dir = os.path.join(os.path.expanduser("~"), ".grape-sandbox-uds")
        os.makedirs(_uds_dir, exist_ok=True)
        self._socket_path = os.path.join(_uds_dir, f"sandbox-{_safe}-{_hash}.sock")

        # 动态白名单：基础设施出站 + grape host + 127.0.0.1 + 额外域名
        # 基础设施出站（LLM API 等）走白名单直通，不走 /api/proxy
        # 业务数据出站（gitcode 等）走 /api/proxy 五维校验
        wl = list(self._whitelist)
        server_url = os.environ.get("GRAPE_SERVER_URL", "") or os.environ.get("RUNTIME_SERVER_URL", "")
        if server_url:
            parsed = urlparse(server_url)
            host = parsed.hostname or ""
            if host and host not in wl:
                wl.append(host)
        if "127.0.0.1" not in wl:
            wl.append("127.0.0.1")
        # 额外白名单域名——从 ConfigSync 读（安全策略，取交集）
        from src.domains.runtime.agents.sandbox.config_sync import get_config_sync
        extra_wl = get_config_sync().get("extra_whitelist")
        if extra_wl:
            for domain in extra_wl.split(","):
                domain = domain.strip()
                if domain and domain not in wl:
                    wl.append(domain)
        # DB engine_sandbox_whitelist 表的官方域名从内存缓存读（异步加载 + 线程锁）
        # 避免每次 start() 都同步查 DB
        _wl_cache = _WhitelistCache.get()
        for domain in _wl_cache.load():
            if domain not in wl:
                wl.append(domain)
        # BUG-14 修复：白名单加载失败时 fail-closed（不启动 NetworkProxy）
        if getattr(_wl_cache, '_load_error', False):
            raise RuntimeError("WhitelistCache load failed — refusing to start NetworkProxy with empty whitelist (fail-closed)")

        # skill egress 策略日志
        if self._egress_policy:
            log.info(
                f"egress policy active: task={task_id} "
                f"skill_id={self._egress_policy.skill_id} "
                f"rules={len(self._egress_policy.rules)} "
                f"digest={self._egress_policy.skill_digest}")
        # feature flag 状态写审计
        egress_enabled = bool(os.environ.get("SANDBOX_EGRESS_ENABLED"))
        if self._audit_log:
            try:
                from src.domains.runtime.agents.common.audit import AuditEvent, AuditKind, now_iso
                self._audit_log.log(AuditEvent(
                    ts=now_iso(),
                    task_id=task_id,
                    kind=AuditKind.NETWORK_DECISION,
                    outcome="egress_config",
                    detail={
                        "egress_enabled": egress_enabled,
                        "has_policy": self._egress_policy is not None,
                        "rules_count": len(self._egress_policy.rules) if self._egress_policy else 0,
                    },
                ))
            except Exception:
                pass

        # 创建 per-instance handler 子类
        # local_gateway 端口级白名单——只允许声明的端口访问回环地址
        _gw_ports: set[int] = set()
        if self._local_gateway:
            try:
                _gw_host, _gw_port_str = self._local_gateway.rsplit(":", 1)
                _gw_port = int(_gw_port_str)
                # fail-closed：local_gateway 必须是回环地址
                if not _is_loopback_host(_gw_host):
                    raise NetworkStartupError(
                        f"local_gateway must be loopback: {self._local_gateway}")
                _gw_ports.add(_gw_port)
                log.info(f"local_gateway: {self._local_gateway} for task={task_id} "
                         f"(allowed loopback ports: {_gw_ports})")
            except NetworkStartupError:
                raise
            except Exception as e:
                raise NetworkStartupError(
                    f"local_gateway parse failed: {self._local_gateway}: {e}")
        handler = type(
            f"Handler_{_hash}",
            (_WhitelistProxyHandler,),
            {
                "_task_id": task_id,
                "_whitelist": wl,
                "_audit_log": self._audit_log,
                "_leaked_tokens": [],
                "_local_gateway_ports": _gw_ports,
                "_egress_policy": self._egress_policy,
                "_rate_limiter": self._rate_limiter,
            },
        )

        try:
            self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            self._server.daemon_threads = True
            self._proxy_port = self._server.server_address[1]
            self._server_thread = threading.Thread(
                target=self._server.serve_forever, daemon=True)
            self._server_thread.start()

            self._bridge = _UnixSocketBridge(self._socket_path, self._proxy_port)
            self._bridge.start()
            # 等 socket 文件创建完成（竞态条件：bwrap --bind 需要文件已存在）
            if not self._bridge._ready.wait(timeout=2):
                raise NetworkStartupError("UnixSocketBridge startup timeout")
            if not os.path.exists(self._socket_path):
                raise NetworkStartupError(f"socket file not created: {self._socket_path}")
        except Exception as e:
            self.stop()
            raise NetworkStartupError(f"proxy start failed: {e}") from e

        log.info(
            f"sandbox proxy started: task={task_id} "
            f"port={self._proxy_port} socket={self._socket_path}")

    def stop(self) -> None:
        """关闭代理 + 桥接。幂等。"""
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
        if self._task_id:
            log.info(f"sandbox proxy stopped: task={self._task_id}")
            self._task_id = ""

    def env(self) -> dict[str, str]:
        """返回代理环境变量。

        --unshare-net 模式：HTTP_PROXY 指向沙箱内 stage2 (127.0.0.1:8888)
        stage2 通过 UDS 转发到宿主机 NetworkProxy。
        """
        if not self.is_running():
            return {}
        # stage2 在沙箱内监听 8888，转发到 UDS → 宿主机 NetworkProxy
        proxy_url = "http://127.0.0.1:8888"
        env = {}
        for key in _PROXY_ENV_KEYS:
            env[key] = proxy_url
        # local_gateway: 从 NO_PROXY 摘除回环地址，让该 host 走代理到宿主回环
        # 前提：已 strace 确认 codex 不访问沙箱内自己的回环（只连 127.0.0.1:40001）
        # 摘除全部等价写法（127.0.0.1 / localhost / ::1），无论 config.toml 写哪种都走代理
        no_proxy = _NO_PROXY_VALUES
        if self._local_gateway:
            for h in _LOOPBACK_HOSTS:
                no_proxy = _remove_from_csv(no_proxy, h)
            if no_proxy != _NO_PROXY_VALUES:
                log.info(f"local_gateway: NO_PROXY 摘除回环 for task={self._task_id} "
                         f"(gateway={self._local_gateway}, no_proxy={no_proxy})")
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        env["GIT_SSH_COMMAND"] = "/bin/false"
        return env

    def is_running(self) -> bool:
        return self._server is not None and self._server_thread is not None

    def socket_mount_path(self) -> str:
        """Unix socket host 侧路径，供 bwrap --bind。"""
        return self._socket_path


# ===== NullNetworkIsolation =====

class NullNetworkIsolation:
    """空网络隔离——完全实现 Protocol，不抛 NotImplementedError。"""

    def start(self, task_id: str) -> None: pass
    def stop(self) -> None: pass
    def env(self) -> dict[str, str]: return {}
    def is_running(self) -> bool: return False
    def socket_mount_path(self) -> str: return ""
