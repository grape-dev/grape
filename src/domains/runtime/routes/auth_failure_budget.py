"""Issue #4347：runtime 鉴权失败 IP 失败预算 + 拒绝日志按 IP 聚合节流。

背景：外部主机用失效 `rta_` 凭据对 worker 协议端点无限重试（实测 741 条/小时，
占 worker WARN 17%），鉴权失败路径此前只写 WARNING，无限流/无封禁。

本模块为纯进程内实现（计数器 + 封禁状态 + 日志聚合器，无新表/迁移）：

- 分钟窗口（60s）内同 IP 鉴权失败 > `RUNTIME_AUTH_FAIL_MINUTE_LIMIT`（默认 10）
  → 调用方返回 429（Retry-After）；
- 小时窗口（3600s）内同 IP 累计失败 > `RUNTIME_AUTH_FAIL_HOUR_LIMIT`（默认 60）
  → 短期封禁 `RUNTIME_AUTH_BAN_SECONDS`（默认 600s），封禁期内直接 429；
- 只计鉴权失败（凭据校验不过）；成功注册/心跳不受影响；
- 封禁期内的请求不再计数（避免封禁被持续重试无限续期）；
- 拒绝日志按 IP 聚合：每 IP 每个 60s 窗口最多 1 条摘要（窗口累计次数 + 首末时间 +
  prefix/endpoint 集合 + 自上一条摘要以来被抑制的条数），摘要保留
  "runtime credential rejected" 前缀以兼容既有 Loki/Grafana 检索。

阈值按进程分摊说明：多 worker/多进程部署时各进程独立计数（Spec 已声明，
重启退化为重新计数，代价可接受）。内存上界 `RUNTIME_AUTH_FAIL_MAX_IPS`
（默认 10000）：超出时先清理过期条目，仍超则整体重置（fail-open：宁可重置
计数，也不让鉴权主链路因计数器内存被拒）。
"""

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from src.kernel.config import log

# 窗口常量（固定滑动窗口宽度，不随 env 改变）
MINUTE_WINDOW_S = 60
HOUR_WINDOW_S = 3600


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """读整型 env 配置，非法值回退默认并告警（对齐 _resolve_probe_fresh_window 模式）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning(f"{name} 非法值 '{raw}'（非整数），回退默认 {default}")
        return default
    if val < lo or val > hi:
        log.warning(f"{name} 非法值 '{raw}'（超出 {lo}~{hi} 范围），回退默认 {default}")
        return default
    return val


def _minute_limit() -> int:
    return _env_int("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", 10, 1, 100000)


def _hour_limit() -> int:
    return _env_int("RUNTIME_AUTH_FAIL_HOUR_LIMIT", 60, 1, 1000000)


def _ban_seconds() -> int:
    return _env_int("RUNTIME_AUTH_BAN_SECONDS", 600, 1, 86400)


def _max_ips() -> int:
    return _env_int("RUNTIME_AUTH_FAIL_MAX_IPS", 10000, 100, 1000000)


def _iso(ts: float) -> str:
    """epoch 秒 → UTC ISO（摘要里的人类可读首末时间）。"""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return str(ts)


class _IpFailState:
    """单 IP 的失败预算 + 日志聚合状态（仅 gate 内部使用，锁内访问）。"""

    __slots__ = ("minute", "hour", "banned_until", "win", "logged_in_win",
                 "agg_count", "agg_first", "agg_last", "agg_prefixes", "agg_endpoints")

    def __init__(self, now: float):
        self.minute: deque = deque()
        self.hour: deque = deque()
        self.banned_until = 0.0
        # 日志聚合：窗口 id（epoch//60）、本窗口是否已发过摘要、
        # 自上一条摘要以来被抑制的累计（count/首末/prefix/endpoint 集合）
        self.win = int(now // MINUTE_WINDOW_S)
        self.logged_in_win = False
        self.agg_count = 0
        self.agg_first = 0.0
        self.agg_last = 0.0
        self.agg_prefixes: set = set()
        self.agg_endpoints: set = set()

    def prune(self, now: float) -> None:
        while self.minute and self.minute[0] <= now - MINUTE_WINDOW_S:
            self.minute.popleft()
        while self.hour and self.hour[0] <= now - HOUR_WINDOW_S:
            self.hour.popleft()

    def stale(self, now: float) -> bool:
        return (not self.minute and not self.hour
                and (not self.banned_until or now >= self.banned_until))


class FailureOutcome:
    """record_failure 的判定结果（调用方据此决定 401/429 与是否输出摘要）。"""

    __slots__ = ("action", "retry_after", "log_line")

    def __init__(self, action: str, retry_after: int = 0, log_line: str = ""):
        self.action = action          # "reject"（401）| "throttle"（分钟超限 429）| "ban"（小时超限，进入短期封禁）
        self.retry_after = retry_after  # >0 → 调用方应返回 429 + Retry-After 头
        self.log_line = log_line        # 非空 → 调用方输出该条聚合摘要（每 IP 每窗口最多 1 条）


class AuthFailureBudget:
    """IP 失败预算（滑动窗口）+ 短期封禁 + 拒绝日志按 IP 聚合节流。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._states: dict = {}

    # ---- 读侧：封禁检查（请求入口快速路径） ----
    def check_banned(self, ip: str, now: float = None) -> int:
        """封禁期内返回剩余秒数（>0），未封禁返回 0。"""
        if not ip:
            return 0
        now = time.time() if now is None else now
        with self._lock:
            st = self._states.get(ip)
            if not st or not st.banned_until or now >= st.banned_until:
                return 0
            return int(st.banned_until - now) + 1

    # ---- 写侧：失败计数 + 阈值判定 + 聚合摘要 ----
    def record_failure(self, ip: str, prefix: str, endpoint: str,
                       now: float = None) -> FailureOutcome:
        """记录一次鉴权失败并判定动作。

        - 分钟窗口内 > minute_limit → "throttle"（429，Retry-After 指向窗口滑出）；
        - 小时窗口内 > hour_limit → "ban"（进入短期封禁，封禁期内入口直接 429）；
        - 其余 → "reject"（调用方按原语义返回 401）。
        封禁期内的失败不计数（防封禁被无限续期），直接返回 "ban"。
        """
        if not ip:
            # 拿不到来源 IP（极端代理场景）不计数：宁可放行 401 也不误伤共享出口
            return FailureOutcome("reject")
        now = time.time() if now is None else now
        with self._lock:
            st = self._states.get(ip)
            if st and st.banned_until and now < st.banned_until:
                return FailureOutcome("ban", int(st.banned_until - now) + 1)
            if st is None:
                self._evict_if_full(now)
                st = _IpFailState(now)
                self._states[ip] = st
            st.prune(now)
            st.minute.append(now)
            st.hour.append(now)
            minute_n = len(st.minute)
            hour_n = len(st.hour)

            # 阈值判定（严格大于：>10/min、>60/h）
            if hour_n > _hour_limit():
                ban_s = _ban_seconds()
                st.banned_until = now + ban_s
                action, retry_after = "ban", ban_s
            elif minute_n > _minute_limit():
                action, retry_after = "throttle", int(MINUTE_WINDOW_S - now % MINUTE_WINDOW_S) + 1
            else:
                action, retry_after = "reject", 0

            # 日志聚合：窗口 id 滚动（新窗口解除“本窗口已发摘要”标记）
            win = int(now // MINUTE_WINDOW_S)
            if st.win != win:
                st.win = win
                st.logged_in_win = False
            # 聚合账本累加当前失败（覆盖自上一条摘要以来的全部被抑制条目）
            st.agg_count += 1
            st.agg_first = st.agg_first or now
            st.agg_last = now
            st.agg_prefixes.add(str(prefix)[:16])
            st.agg_endpoints.add(str(endpoint)[:80])

            log_line = ""
            # 每窗口最多 1 条摘要；ban 是一次性状态迁移，强制可见（不被窗口节流吞掉）
            if action == "ban" or not st.logged_in_win:
                st.logged_in_win = True
                log_line = (
                    f"runtime credential rejected: ip={ip} window_count={st.agg_count}"
                    f" first={_iso(st.agg_first)} last={_iso(st.agg_last)}"
                    f" prefixes={','.join(sorted(st.agg_prefixes))}"
                    f" endpoints={','.join(sorted(st.agg_endpoints))}"
                    f" minute={minute_n} hour={hour_n} action={action}")
                # 摘要已发出，聚合账本清零（下一条摘要从新账本累计）
                st.agg_count = 0
                st.agg_first = 0.0
                st.agg_prefixes = set()
                st.agg_endpoints = set()
            return FailureOutcome(action, retry_after, log_line)

    def _evict_if_full(self, now: float) -> None:
        """条目数达上界：先清过期项，仍满则整体重置（fail-open，保鉴权主链路）。"""
        if len(self._states) < _max_ips():
            return
        for _ip in [k for k, v in self._states.items() if v.stale(now)]:
            del self._states[_ip]
        if len(self._states) >= _max_ips():
            log.warning(f"auth failure budget: tracked ips reached cap, reset counters (fail-open)")
            self._states.clear()

    def reset(self) -> None:
        """清空全部状态（测试用）。"""
        with self._lock:
            self._states.clear()


# 进程级单例：4 进程各自独立计数（阈值按进程量级评估，见模块头说明）
auth_failure_gate = AuthFailureBudget()
