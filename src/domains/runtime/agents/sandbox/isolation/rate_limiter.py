"""Per-task 速率限制器。

两个维度：
1. 每 task 每 host 的请求数上限：默认 100 次/分钟（滑动窗口）
2. 每 task 的总出站字节上限：默认 100MB

窗口类型：滑动窗口（deque 存时间戳，60s 窗口，清理过期后计数）。
超限断连 + 审计告警。

字节计数方向：outbound = request_body + response_body（出站方向）。
host 键：CONNECT/请求行里的 host（域名级，不按解析 IP）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("devops-bot")

_DEFAULT_MAX_RPM = 100
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100MB


class RateLimiter:
    """per-task 速率限制器。

    线程安全（ThreadingTCPServer 并发处理时多个线程共享）。
    """

    def __init__(self, task_id: str,
                 max_requests_per_minute: int = _DEFAULT_MAX_RPM,
                 max_total_bytes: int = _DEFAULT_MAX_BYTES):
        self._task_id = task_id
        self._max_rpm = max_requests_per_minute
        self._max_bytes = max_total_bytes
        self._request_counts: dict[str, deque] = {}
        self._total_bytes = 0
        self._lock = threading.Lock()

    def check_request(self, host: str) -> tuple[bool, str]:
        """检查请求速率。返回 (allowed, reason)。

        滑动窗口：保留 60s 内的时间戳，超限拒绝。
        """
        now = time.monotonic()
        with self._lock:
            timestamps = self._request_counts.get(host)
            if timestamps is None:
                timestamps = deque()
                self._request_counts[host] = timestamps
            # 清理 60s 前的记录
            while timestamps and now - timestamps[0] >= 60:
                timestamps.popleft()
            if len(timestamps) >= self._max_rpm:
                return False, f"rate limit exceeded: {len(timestamps)}/min for {host}"
            timestamps.append(now)
            return True, "ok"

    def check_bytes(self, byte_count: int) -> tuple[bool, str]:
        """检查总出站字节。返回 (allowed, reason)。

        byte_count 是本次传输的字节数（request_body 或 response_body）。
        累计超过 max_total_bytes → 拒绝。
        """
        with self._lock:
            self._total_bytes += byte_count
            if self._total_bytes > self._max_bytes:
                return False, f"byte limit exceeded: {self._total_bytes}/{self._max_bytes}"
            return True, "ok"
