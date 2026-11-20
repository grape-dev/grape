"""DevBox Gateway DNS override——进程级 monkey-patch socket.getaddrinfo。

把 *.devbox-s2.hwtunnel.com 的 DNS 请求映射到固定 IP。
保留 HTTP Host、SNI 和证书校验（不改 URL，只改 DNS 解析）。
"""
import socket
from fnmatch import fnmatchcase
import logging

log = logging.getLogger(__name__)

_original_getaddrinfo = socket.getaddrinfo
_gateway_ip: str | None = None
_gateway_pattern: str | None = None


def init_dns_override(gateway_ip: str, gateway_domain: str = "devbox-s2.hwtunnel.com"):
    """初始化进程级 DNS override。

    把 *.{gateway_domain} 的 DNS 请求映射到固定 IP。
    保留 HTTP Host、SNI 和证书校验（不改 URL，只改 DNS 解析）。

    BUG-10 修复：支持 gateway_ip 变更更新（配置刷新后生效）。

    Args:
        gateway_ip: Gateway IP 地址（例如 "203.0.113.10"，文档保留地址段）
        gateway_domain: 隧道域名后缀（例如 "devbox-s2.hwtunnel.com"）
    """
    global _gateway_ip, _gateway_pattern
    _gateway_ip = gateway_ip
    _gateway_pattern = f"*{gateway_domain}"
    if socket.getaddrinfo is _original_getaddrinfo:
        socket.getaddrinfo = _patched_resolve
        log.info(f"devbox DNS override: *.{gateway_domain} -> {gateway_ip}")
    else:
        log.info(f"devbox DNS override updated: *.{gateway_domain} -> {gateway_ip}")


def _patched_resolve(host, port, *args, **kwargs):
    name = host.decode("ascii") if isinstance(host, bytes) else host
    if name and _gateway_pattern and fnmatchcase(name.lower(), _gateway_pattern):
        host = _gateway_ip
    return _original_getaddrinfo(host, port, *args, **kwargs)


def cleanup_dns_override():
    """恢复原始 DNS 解析。"""
    global _gateway_ip, _gateway_pattern
    _gateway_ip = None
    _gateway_pattern = None
    if socket.getaddrinfo is not _original_getaddrinfo:
        socket.getaddrinfo = _original_getaddrinfo
        log.info("devbox DNS override cleaned up")
