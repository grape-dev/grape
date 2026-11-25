"""窗口日志去重（Issue #3759）。

背景：上游 VCS 的永久错误（4xx，429 除外）在「孤儿仓库卡」上会被逐轮巡检
无限放大——`huaweicloud-mate/repository-requests` 的 issues 62/63 每轮各打一次
上游 GET，404（永久错误，不可自愈）被当成可重试错误，一小时 76 条 WARNING，
且每条都带完整上游 URL 与响应体，把同仓库的真实告警淹掉。

本模块只做一件事：**按 key 的固定窗口去重**（不是永久去重）。

- 窗口内首条：``emit_full=True`` —— 调用方输出完整事件（含上游响应体）；
- 窗口内后续：``emit_full=False`` —— 调用方只计入本地计数，不打日志（防刷屏）；
- 窗口滚动（同一 key 在窗口过期后再次到达）：``emit_full=True`` 且
  ``folded>0`` —— 调用方据此先补一条上个窗口的汇总（"60s 内 N 次相同 404"），
  再输出新的完整首条。

为什么保留窗口滚动而不是永久抑制：同一 key 的失败也可能周期性复发
（上游恢复一阵又挂），永久抑制会把「反复失败」静默掉。窗口滚动保证每个窗口
至少有一条可统计的计数记录。

本模块只依赖标准库，位置在 ``src/kernel``（arch 规则 R1/R2：kernel 不反向依赖
domains/api）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# 默认窗口：60s。为何是 60s：与各巡检/回填的轮询周期同量级，能把「一轮里
# 并发打同一上游」的突发（issue 复核 8 并发）压成 1 条，又不掩盖跨轮复发。
DEFAULT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class DedupeDecision:
    """一次 ``note()`` 的判定结果。"""

    key: str
    emit_full: bool
    #: 被折叠的条数。``emit_full=False`` 时是本窗口内累计折叠数（含本次）；
    #: ``emit_full=True`` 且窗口刚滚动时，是**上一个窗口**折叠掉的总数（0=新 key）。
    folded: int
    #: 当前窗口的起点（单调时钟），便于调用方换算窗口长度。
    window_start: float


class WindowDeduper:
    """线程安全的按 key 固定窗口去重器。

    ``clock`` 可注入（测试用假时钟，避免 sleep）。
    """

    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 clock: Optional[Callable[[], float]] = None):
        try:
            win = float(window_seconds)
        except (TypeError, ValueError):
            win = DEFAULT_WINDOW_SECONDS
        self.window_seconds = win if win > 0 else DEFAULT_WINDOW_SECONDS
        self._clock = clock or time.monotonic
        # key -> (window_start, 本窗口事件数, 本窗口已折叠数)
        self._state: dict[str, tuple[float, int, int]] = {}
        self._lock = threading.Lock()

    def note(self, key: str, now: Optional[float] = None) -> DedupeDecision:
        """记录一次事件并给出是否输出完整日志的判定。"""
        _now = self._clock() if now is None else now
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                self._state[key] = (_now, 1, 0)
                return DedupeDecision(key, True, 0, _now)
            start, count, folded = entry
            if _now - start >= self.window_seconds:
                # 窗口滚动：先把上一窗口的折叠数交给调用方补汇总，再重置为本窗口首条。
                self._state[key] = (_now, 1, 0)
                return DedupeDecision(key, True, folded, _now)
            # 窗口内后续：只计数，不输出。
            self._state[key] = (start, count + 1, folded + 1)
            return DedupeDecision(key, False, folded + 1, start)

    def reset(self, key: Optional[str] = None) -> None:
        """清空某个 key（或全部）的窗口状态（测试/运维用）。"""
        with self._lock:
            if key is None:
                self._state.clear()
            else:
                self._state.pop(key, None)

    def active_keys(self) -> list[str]:
        """当前进程内有窗口状态的 key（排障用，不触发任何副作用）。"""
        with self._lock:
            return sorted(self._state)

    def folded_total(self, key: str) -> int:
        """当前窗口内该 key 的累计折叠数（0 表示无窗口/未折叠）。"""
        with self._lock:
            entry = self._state.get(key)
        return entry[2] if entry else 0