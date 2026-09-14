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
import threading
from pathlib import Path

log = logging.getLogger("devops-bot")

_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_ROTATED = 5


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
        """
        with self._lock:
            prev_hash = self._chain_state.get(self._path, "")
            payload = dict(record)
            payload["prev_hash"] = prev_hash
            record_hash = self._compute_hash(payload)
            payload["record_hash"] = record_hash
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._append(line)
            self._chain_state[self._path] = record_hash

    def _compute_hash(self, payload: dict) -> str:
        """HMAC-SHA256。"""
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hmac.new(self._hmac_key, data, hashlib.sha256).hexdigest()

    def _append(self, line: str) -> None:
        """追加写入 + 轮转检查。"""
        current = os.path.getsize(self._path) if os.path.exists(self._path) else 0
        if current > _MAX_BYTES:
            self._rotate()
        with open(self._path, "a") as f:
            f.write(line)

    def _rotate(self) -> None:
        """轮转：audit.jsonl → .1 → .2 → ... → 删除最老的。"""
        for i in range(_MAX_ROTATED - 1, 0, -1):
            old = f"{self._path}.{i}"
            new = f"{self._path}.{i + 1}"
            if os.path.exists(old):
                os.rename(old, new)
        if os.path.exists(self._path):
            os.rename(self._path, f"{self._path}.1")
        log.info(f"audit: rotated {self._path}")

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
