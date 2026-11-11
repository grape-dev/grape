"""Issue #516 M2：任务进度快照（checkpoint）落盘 / 恢复。

目的：长任务被超时杀死前，把执行进度（最近流式事件摘要）周期性原子落盘；
自动重试时加载快照注入续跑上下文，避免上一轮（最长 1-2 小时）工作全部作废。

设计要点（docs/issue-516/plan.md）：
- **原子写**：先写 `<path>.tmp` 再 `os.replace` rename——进程被杀（超时 kill）时
  文件要么是上一版完整快照、要么不存在，不会出现半截损坏文件被当成有效快照。
- **降级从零跑**：加载失败（损坏/缺字段）返回 None，调用方视为无快照。
- **生命周期**：成功/异常退出路径由 `TaskResourceStack.dispose()` 统一清理；
  超时取消路径（CancelledError 不走 dispose）保留文件供重试续跑。
- **轻量**：内存只留最近 `tail` 条事件摘要（每条文本截断），落盘间隔默认 60s，
  对流式主链路无感知（feed 是纯内存 append + 到点同步小文件写）。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

#: 默认落盘间隔（秒）——超时被杀前至少有一个可用快照的粒度
DEFAULT_FLUSH_INTERVAL = 60.0

#: 内存保留的最近事件条数（快照里只带尾部摘要）
DEFAULT_TAIL = 40

#: 单条事件文本截断长度（防单条超大 payload 撑爆快照）
_EVENT_TEXT_MAX = 400

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _checkpoint_dir(base_dir: Optional[str] = None) -> str:
    if base_dir:
        return base_dir
    env_dir = os.environ.get("GRAPE_CHECKPOINT_DIR", "")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".cache", "grape-task-checkpoints")


def checkpoint_path(task_id: str, base_dir: Optional[str] = None) -> str:
    """快照文件路径：`<dir>/<sanitized task_id>.json`。"""
    tid = _SAFE_ID.sub("_", str(task_id or "task"))[:120] or "task"
    return os.path.join(_checkpoint_dir(base_dir), f"{tid}.json")


def _clip(data: Any) -> str:
    """事件 payload → 截断文本（dict 尽力 json 序列化，失败退 str）。"""
    if isinstance(data, str):
        s = data
    else:
        try:
            s = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            s = str(data)
    return s[:_EVENT_TEXT_MAX]


class TaskCheckpoint:
    """单任务进度快照：feed 进度事件 → 周期原子落盘 → 重试加载。"""

    def __init__(self, task_id: str, interval: float = DEFAULT_FLUSH_INTERVAL,
                 tail: int = DEFAULT_TAIL, base_dir: Optional[str] = None):
        self.task_id = str(task_id or "")
        self.interval = float(interval) if interval and interval > 0 else DEFAULT_FLUSH_INTERVAL
        self.path = checkpoint_path(task_id, base_dir)
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max(1, int(tail)))
        self._event_count = 0
        self._last_flush = 0.0

    # ---- 写侧 ----

    def feed(self, evt_type: str, data: Any) -> None:
        """喂一条流式事件（内存 append；到落盘间隔则同步 flush）。"""
        self._events.append({"type": str(evt_type or ""), "text": _clip(data)})
        self._event_count += 1
        if time.monotonic() - self._last_flush >= self.interval:
            self.flush()

    def flush(self) -> bool:
        """原子落盘当前快照（temp + rename）。失败静默（快照是尽力而为的优化）。"""
        self._last_flush = time.monotonic()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {
                "task_id": self.task_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "event_count": self._event_count,
                "events": list(self._events),
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    def clear(self) -> None:
        """清理快照文件（任务终态成功后调用；幂等）。"""
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    # ---- 读侧 ----

    def load(self) -> Optional[Dict[str, Any]]:
        """加载快照；不存在/损坏/缺字段 → None（降级从零跑）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict) or "events" not in data:
            return None
        return data

    def resume_context(self, snap: Optional[Dict[str, Any]] = None) -> str:
        """把快照格式化为注入 prompt 的续跑上下文块（无快照 → 空串）。"""
        data = snap if snap is not None else self.load()
        if not data:
            return ""
        events: List[Dict[str, Any]] = [e for e in (data.get("events") or [])
                                        if isinstance(e, dict)]
        lines = []
        for i, e in enumerate(events, 1):
            lines.append(f"{i}. [{e.get('type') or 'event'}] {e.get('text') or ''}")
        tail_txt = "\n".join(lines)
        return (
            "\n\n[grape runtime] NOTE: a previous attempt of this task timed out and was "
            f"auto-retried. Progress snapshot ({data.get('event_count', len(events))} events so far, "
            f"latest {len(events)} shown, newest last, saved at {data.get('updated_at', '?')}):\n"
            f"{tail_txt}\n"
            "Continue from this progress instead of redoing completed steps where practical."
        )


def inject_resume(task: Dict[str, Any], env: Dict[str, str], prompt: str,
                  ckpt: "TaskCheckpoint") -> Tuple[str, bool]:
    """Issue #516 M2：重试续跑注入（_execute_task 调用，便于单测）。

    task 带 `_grape_resume_attempt >= 2`（_wrap 重试轮次打的标记）时：
    加载快照 → prompt 追加续跑上下文 + env 注入 `GRAPE_RESUME_CHECKPOINT`。
    快照损坏/缺失 → 原样返回 prompt（降级从零跑）。返回 (prompt, resumed)。
    """
    try:
        if int(task.get("_grape_resume_attempt") or 0) < 2:
            return prompt, False
        snap = ckpt.load()
        if not snap:
            return prompt, False
        ctx = ckpt.resume_context(snap)
        if not ctx:
            return prompt, False
        env.setdefault("GRAPE_RESUME_CHECKPOINT", ckpt.path)
        return prompt + ctx, True
    except Exception:
        return prompt, False
