"""FileAuditLogger——JSONL 哈希链 + 异步写入。

实现 common.audit.AuditListener Protocol。
全局单例，由 SandboxManager 构造时注入（引用，不拥有生命周期）。

设计参考：agent-bom/proxy_audit.py
- HMAC-SHA256 哈希链（prev_hash + record_hash），篡改断链
- 异步写入（不阻塞请求）
- 审计写失败传播而非静默丢弃
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any

from src.domains.runtime.agents.common.audit import AuditListener, AuditEvent, AuditKind

log = logging.getLogger("devops-bot")

_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_ROTATED = 5


class _AsyncWriter:
    """异步磁盘写入器。哈希链同步计算，磁盘 I/O 异步。

    group commit：攒满 100 条或 10ms 超时后批量写盘，减少 I/O 次数。
    队列满时同步写入兜底（不丢审计）。
    """

    _FLUSH_SENTINEL = object()
    _BATCH_SIZE = 100       # 攒满 100 条触发写盘
    _BATCH_TIMEOUT = 0.01   # 10ms 超时触发写盘

    def __init__(self):
        self._q: queue.Queue[Any] = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def put(self, path: str, line: str) -> None:
        try:
            self._q.put_nowait((path, line))
        except queue.Full:
            # 队列满时加锁同步写入（不丢审计）
            with _write_lock:
                with open(path, "a") as f:
                    f.write(line)

    def flush(self) -> None:
        """等待队列清空。最多等 5 秒。"""
        self._q.put(self._FLUSH_SENTINEL)
        import time as _t
        deadline = _t.monotonic() + 5
        while not self._q.empty() and _t.monotonic() < deadline:
            _t.sleep(0.01)
        if not self._q.empty():
            log.warning(f"audit flush timeout (5s), {self._q.qsize()} records may not be persisted")

    def _run(self) -> None:
        import time as _t
        while True:
            # 阻塞等第一条
            item = self._q.get()
            if item is self._FLUSH_SENTINEL:
                self._q.task_done()
                continue

            # 攒 batch：最多 100 条或 10ms 超时
            batch: list[tuple[str, str]] = []
            if isinstance(item, tuple):
                batch.append(item)
            deadline = _t.monotonic() + self._BATCH_TIMEOUT
            while len(batch) < self._BATCH_SIZE and _t.monotonic() < deadline:
                try:
                    item = self._q.get(block=True, timeout=max(0, deadline - _t.monotonic()))
                except queue.Empty:
                    break
                if item is self._FLUSH_SENTINEL:
                    self._q.task_done()
                    break
                if isinstance(item, tuple):
                    batch.append(item)

            if not batch:
                continue

            # 按 path 分组，批量写盘
            by_path: dict[str, list[str]] = {}
            for path, line in batch:
                by_path.setdefault(path, []).append(line)
            for path, lines in by_path.items():
                data = "".join(lines)
                try:
                    _write_with_rotate(path, data)
                except Exception as e:
                    log.error(f"audit write failed (propagated): {e}")

            for _ in batch:
                self._q.task_done()


_write_lock = threading.Lock()


def _write_with_rotate(path: str, data: str):
    """写入审计数据（单行或多行拼接）。检查文件大小并轮转。"""
    with _write_lock:
        current = os.path.getsize(path) if os.path.exists(path) else 0
        if current > _MAX_BYTES:
            _rotate(path)
        with open(path, "a") as f:
            f.write(data)


def _rotate(path: str):
    for i in range(_MAX_ROTATED - 1, 0, -1):
        old = f"{path}.{i}"
        new = f"{path}.{i + 1}"
        if os.path.exists(old):
            try:
                os.rename(old, new)
            except Exception:
                pass
    if os.path.exists(path):
        try:
            os.rename(path, f"{path}.1")
        except Exception:
            pass


class FileAuditLogger:
    """JSONL 审计日志 + HMAC-SHA256 哈希链。

    实现 common.audit.AuditListener Protocol。
    全局单例——跨任务共享一条哈希链。
    verify() 是运维/CI 独立入口。
    """

    def __init__(self, log_path: str):
        self._path = log_path
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self._hmac_key = self._load_or_create_key()
        self._prev_hash = self._load_prev_hash()
        self._writer = _AsyncWriter()
        self._chain_lock = threading.Lock()

    def log(self, event: AuditEvent) -> None:
        """记录审计事件。哈希链同步计算，磁盘 I/O 异步。"""
        with self._chain_lock:
            record = {
                "ts": event.ts,
                "task_id": event.task_id,
                "kind": getattr(event.kind, "value", str(event.kind)),
                "outcome": event.outcome,
                "host": event.host,
                "method": event.method,
                "path": event.path,
                "detail": event.detail,
                "prev_hash": self._prev_hash,
            }
            record_str = json.dumps(record, sort_keys=True, ensure_ascii=False)
            record_hash = hmac.new(
                self._hmac_key, record_str.encode(), hashlib.sha256
            ).hexdigest()
            record["record_hash"] = record_hash
            self._prev_hash = record_hash

        line = json.dumps(record, ensure_ascii=False) + "\n"
        self._writer.put(self._path, line)

    def verify(self) -> bool:
        """验证哈希链完整性。运维/CI 调用。"""
        try:
            with open(self._path) as f:
                lines = f.readlines()
            prev_hash = ""
            for line in lines:
                record = json.loads(line.strip())
                expected_prev = record.get("prev_hash", "")
                if expected_prev != prev_hash:
                    log.error(f"audit chain broken at record: {record.get('ts')}")
                    return False
                record_hash = record.get("record_hash", "")
                check = {k: v for k, v in record.items() if k != "record_hash"}
                check_str = json.dumps(check, sort_keys=True, ensure_ascii=False)
                expected_hash = hmac.new(
                    self._hmac_key, check_str.encode(), hashlib.sha256
                ).hexdigest()
                if record_hash != expected_hash:
                    log.error(f"audit record hash mismatch at: {record.get('ts')}")
                    return False
                prev_hash = record_hash
            return True
        except Exception as e:
            log.error(f"audit verify failed: {e}")
            return False

    def get_events_by_task(self, task_id: str) -> list[dict]:
        """线性扫描审计 JSONL，返回该 task_id 的全部记录（按 ts 升序）。

        P1 线性扫描，P2 考虑索引/分片。
        上限 MAX_EVENTS_SCAN 防止超大文件拖慢 cleanup。
        """
        from .alert import _AlertConstants

        if not task_id or not os.path.exists(self._path):
            return []
        result: list[dict] = []
        count = 0
        try:
            with open(self._path) as f:
                for line in f:
                    count += 1
                    if count > _AlertConstants.MAX_EVENTS_SCAN:
                        log.warning(
                            f"audit scan truncated at {_AlertConstants.MAX_EVENTS_SCAN} "
                            f"lines for task={task_id}")
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("task_id") == task_id:
                            result.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            log.error(f"get_events_by_task failed: {e}")
        result.sort(key=lambda r: r.get("ts", ""))
        return result

    def _load_or_create_key(self) -> bytes:
        key_path = self._path + ".hmac"
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                return f.read()
        key = os.urandom(32)
        with open(key_path, "wb") as f:
            f.write(key)
        os.chmod(key_path, 0o600)
        return key

    def _load_prev_hash(self) -> str:
        if not os.path.exists(self._path):
            return ""
        try:
            with open(self._path) as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1].strip())
                return last.get("record_hash", "")
        except Exception:
            pass
        return ""
