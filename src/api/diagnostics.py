"""事件循环卡死诊断（2026-11-05 事故复盘后的常驻能力）。

事故现场：`/api/health` 180s 无响应、连静态文件都超时、3000 accept 队列积压满，
14 个线程全在 futex 等待（等锁），MySQL 无长查询、日志无 traceback → 无法定位到源码行。

本模块提供两件事：
1. `kill -USR1 <pid>` 随时 dump 所有 Python 线程栈（faulthandler，无需 ptrace/root）；
2. 事件循环 stall 看门狗：心跳线程每 1s 打点，看门狗每 2s 检查，超过阈值
   （默认 10s，`GRAPE_LOOP_STALL_SECS` 可调）就自动 dump 全线程栈到日志，
   同一轮卡死只 dump 一次（60s 冷却），恢复后再卡会重新 dump。
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import sys
import threading
import time

STALL_SECS = float(os.environ.get("GRAPE_LOOP_STALL_SECS", "10"))
_DUMP_COOLDOWN = 60.0

_beat = time.monotonic()
_installed = False


def install(log: logging.Logger | None = None) -> None:
    """在 lifespan 启动时调用一次（幂等）。"""
    global _installed, _beat
    if _installed:
        return
    _installed = True
    log = log or logging.getLogger(__name__)

    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        log.info("[diag] faulthandler ready: kill -USR1 <pid> dumps python stacks")
    except Exception as e:                      # 非 POSIX / 信号被占用
        log.warning(f"[diag] faulthandler register failed: {e}")

    async def _heartbeat() -> None:
        global _beat
        while True:
            _beat = time.monotonic()
            await asyncio.sleep(1.0)

    try:
        asyncio.get_running_loop().create_task(_heartbeat())
    except Exception as e:
        log.warning(f"[diag] heartbeat task failed: {e}")
        return

    def _watch() -> None:
        last_dump = 0.0
        while True:
            time.sleep(2.0)
            gap = time.monotonic() - _beat
            if gap < STALL_SECS:
                continue
            now = time.monotonic()
            if now - last_dump < _DUMP_COOLDOWN:
                continue
            last_dump = now
            try:
                log.error(f"[diag] event loop stalled {gap:.0f}s — dumping all thread stacks")
            except Exception:
                pass
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                sys.stderr.flush()
            except Exception:
                pass

    threading.Thread(target=_watch, name="diag-loop-watch", daemon=True).start()
    log.info(f"[diag] loop watchdog on (stall>{STALL_SECS:.0f}s → auto dump)")


__all__ = ["install", "STALL_SECS"]