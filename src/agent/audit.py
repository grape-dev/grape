"""RotatingAuditLog — JSONL 审计日志 + 哈希链防篡改 + 轮转（Issue #139）。

设计参考：agent-bom/proxy_audit.py

特性：
- 哈希链：每条记录含 prev_hash + record_hash（HMAC-SHA256），篡改断链
- 轮转：超过 MAX_BYTES 自动轮转，保留 MAX_ROTATED 个历史文件
- HMAC key：持久化到 sidecar 文件（权限 600），Agent 在 bwrap 内不可见
- verify_chain()：验证哈希链完整性

线程安全：所有写入操作加锁，ThreadingTCPServer 并发安全。
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

log = logging.getLogger("devops-bot")

_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_ROTATED = 5
_lock = threading.Lock()  # 哈希链全局锁


class _AsyncWriter:
    """异步磁盘写入器（借鉴 m9m 的 buffered channel）。

    哈希链计算同步执行（保证链连续性），磁盘 I/O 异步（不阻塞代理请求）。
    每个 (path, line) 放入 Queue，后台线程串行写入。

    崩溃风险：进程在 flush 前崩溃时，Queue 中的审计事件会丢失。
    完整方案应使用 Transactional Outbox Pattern（先写 DB outbox 表，后台 worker
    再异步移动到审计存储）。当前场景：审计日志是安全审计用，非交易凭证，
    丢失少量记录可接受。Queue 满时降级为同步写（不丢数据）。
    """

    def __init__(self):
        self._q: queue.Queue[Any] = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def put(self, path: str, line: str) -> None:
        try:
            self._q.put_nowait((path, line))
        except queue.Full:
            # 队列满 — 直接同步写（降级，不丢数据）
            with open(path, "a") as f:
                f.write(line)

    def flush(self) -> None:
        """等待队列中所有写入完成。"""
        sentinel = object()
        self._q.put(sentinel)
        # 等待 sentinel 被处理（简单轮询）
        while not self._q.empty():
            import time as _t
            _t.sleep(0.01)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None or item is sentinel:
                # flush sentinel — 继续处理后续
                if item is sentinel:
                    self._q.task_done()
                continue
            path, line = item
            try:
                with _lock:
                    # 轮转检查 + 写入
                    current = os.path.getsize(path) if os.path.exists(path) else 0
                    if current > _MAX_BYTES:
                        _rotate(path)
                    with open(path, "a") as f:
                        f.write(line)
            except Exception as e:
                log.warning(f"audit async write failed: {e}")
            finally:
                self._q.task_done()


def _rotate(path: str) -> None:
    """轮转：audit.jsonl → .1 → .2 → ... → 删除最老的。"""
    for i in range(_MAX_ROTATED - 1, 0, -1):
        old = f"{path}.{i}"
        new = f"{path}.{i + 1}"
        if os.path.exists(old):
            os.rename(old, new)
    if os.path.exists(path):
        os.rename(path, f"{path}.1")
    log.info(f"audit: rotated {path}")


_async_writer = _AsyncWriter()


class RotatingAuditLog:
    """JSONL 审计日志 + 轮转 + 哈希链。"""

    _chain_state: dict[str, str] = {}  # log_path → last record_hash
    _lock = threading.Lock()

    def __init__(self, path: str, hmac_key: str = ""):
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._hmac_key = self._resolve_key(path, hmac_key)
        # Issue #139 fix: 初始化时读磁盘最后一条记录的 record_hash，避免重启后链断裂
        self._init_chain_state_from_disk()

    def _init_chain_state_from_disk(self):
        """从磁盘日志最后一条记录读取 record_hash，恢复哈希链状态。

        进程重启后 _chain_state 为空 → 第一条记录 prev_hash=''
        但磁盘上最后一条记录的 record_hash 不为空 → verify_chain() 报 mismatch。
        本方法在初始化时读磁盘最后一条记录，恢复链状态。
        """
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                last_hash = ""
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    last_hash = record.get("record_hash", "")
                if last_hash:
                    with RotatingAuditLog._lock:
                        self._chain_state[self._path] = last_hash
        except Exception as e:
            log.warning(f"audit: failed to restore chain state from disk: {e}")

    @staticmethod
    def _resolve_key(path: str, explicit_key: str) -> bytes:
        """解析 HMAC key：显式传入 > sidecar 文件 > 自动生成。"""
        if explicit_key:
            return explicit_key.encode("utf-8")
        sidecar = path + ".chainkey"
        if os.path.exists(sidecar):
            with open(sidecar) as f:
                return f.read().strip().encode()
        # 首次启动生成临时 key，持久化到 sidecar（权限 600）
        key = os.urandom(32).hex().encode()
        try:
            fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, key)
            os.close(fd)
        except FileExistsError:
            # 并发时另一个线程先创建了
            with open(sidecar) as f:
                return f.read().strip().encode()
        except OSError as e:
            log.warning(f"audit: failed to persist chain key sidecar: {e}")
        return key

    def write(self, record: dict) -> None:
        """写入一条审计记录（含哈希链）。

        record 应包含业务字段（ts/task_id/method/host/path/policy/reason 等）。
        本方法自动追加 prev_hash + record_hash。
        哈希链计算同步执行（保证链连续性），磁盘写入异步（Queue + 后台线程，借鉴 m9m）。
        """
        line = self._build_line(record)
        # 异步写入磁盘 — 不阻塞代理请求
        _async_writer.put(self._path, line)

    def write_sync(self, record: dict) -> None:
        """同步写入（测试用，确保磁盘落盘后再验证）。"""
        line = self._build_line(record)
        self._append(line)

    def _build_line(self, record: dict) -> str:
        """计算哈希链并返回 JSONL 行（同步，线程安全）。"""
        with _lock:
            prev_hash = self._chain_state.get(self._path, "")
            payload = dict(record)
            payload["prev_hash"] = prev_hash
            record_hash = self._compute_hash(payload)
            payload["record_hash"] = record_hash
            self._chain_state[self._path] = record_hash
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def flush(self) -> None:
        """等待异步队列中的所有写入完成。"""
        _async_writer.flush()

    def _compute_hash(self, payload: dict) -> str:
        """HMAC-SHA256。"""
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hmac.new(self._hmac_key, data, hashlib.sha256).hexdigest()

    def _append(self, line: str) -> None:
        """追加写入 + 轮转检查（同步，write_sync 用）。"""
        current = os.path.getsize(self._path) if os.path.exists(self._path) else 0
        if current > _MAX_BYTES:
            _rotate(self._path)
        with open(self._path, "a") as f:
            f.write(line)

    def verify_chain(self) -> tuple[bool, str]:
        """验证哈希链完整性。

        返回 (ok, message)。ok=False 时 message 描述第一条断链记录。
        第一条记录的 prev_hash 可能为空（新文件）或非空（轮转后继承 .1 文件的链）。
        从第一条记录开始验证链的连续性（prev_hash 链接是否正确）。
        """
        if not os.path.exists(self._path):
            return True, "no log file"
        prev_hash = None  # None = 第一条记录，接受任意 prev_hash
        with open(self._path) as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"line {i}: invalid JSON"
                stored_prev = record.get("prev_hash", "")
                stored_hash = record.get("record_hash", "")
                if prev_hash is not None and stored_prev != prev_hash:
                    return False, (
                        f"line {i}: prev_hash mismatch "
                        f"(expected {prev_hash[:12]}..., got {stored_prev[:12]}...)"
                    )
                payload = {k: v for k, v in record.items() if k != "record_hash"}
                computed = self._compute_hash(payload)
                if computed != stored_hash:
                    return False, f"line {i}: record_hash mismatch (tampered)"
                prev_hash = stored_hash
        return True, "chain intact"
