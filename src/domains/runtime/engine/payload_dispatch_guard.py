"""同一 payload 的重复投递熔断 + 死信台账（Issue #3818）。

生产实测：agent 每次拒绝凭据外传载荷，fake completion guard 把 exit 0 降级为
failed，autopilot 下一 tick 立刻新建 exec 再投同一份载荷（1 小时 66 次、88+ 轮），
调度侧没有任何 dead-letter / 熔断 / 升级告警。本模块给出跨进程可查的熔断台账：

- 键 = ``payload_guard.payload_hash``（归一化指令的 SHA-256），抗空白/编码/大小写改写；
- ``record_dispatch`` 在**建 execution 之前**调用：已进 dead-letter 即返回
  ``allowed=False``，调度器据此跳过建 exec、把任务标记为不可派发并告警；
- ``record_failure`` 由 fake completion guard 降级 failed（及调度侧非环境性失败）时调用：
  同一载荷累计失败/拒绝达到阈值即进 dead-letter，避免「拒绝 → 重试」被无限放大。
  **熔断只由失败驱动**：正常周期任务天天同指令（从未失败）不应被停派（Spec Risks）。
- 台账行保留 ``status=dead_letter`` 与 ``reason``，执行/死信记录可直接 SQL 查询。

表归属 runtime（configs/table_ownership.json）。调度器走
``db_registry.cursor_for("runtime")``，因此建表也在这条连接上惰性完成——runtime 域的
表由 mgmt 进程的注册式迁移盖章，worker 侧等不到迁移（与 #3527 / webhook_inbox 同因）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from src.kernel.config import log
from src.kernel.db_registry import cursor_for as _rt_cursor_for

TABLE = "autopilot_payload_guard"

STATUS_ACTIVE = "active"
STATUS_DEAD_LETTER = "dead_letter"

REASON_REPEAT_REJECTION = "payload_repeat_rejection"

# autopilot 的合成 event_seq 命名空间（与 scheduler._AUTOPILOT_EVENT_SEQ_BASE 一致）。
# 只有 autopilot 执行才参与熔断：issue/coding 的正常事件任务不按载荷计数。
AUTOPILOT_EVENT_SEQ_BASE = 900_000_000

_ENV_ENABLED = "GRAPE_AUTOPILOT_PAYLOAD_GUARD_ENABLED"
_ENV_LIMIT = "GRAPE_AUTOPILOT_PAYLOAD_DISPATCH_LIMIT"
DEFAULT_LIMIT = 3

DDL = """
CREATE TABLE IF NOT EXISTS autopilot_payload_guard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_hash VARCHAR(64) NOT NULL,
    task_id INTEGER NOT NULL DEFAULT 0,
    repo_key VARCHAR(255) NOT NULL DEFAULT '',
    dispatch_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    reason VARCHAR(128) NOT NULL DEFAULT '',
    first_seen_at VARCHAR(32) NOT NULL DEFAULT '',
    last_seen_at VARCHAR(32) NOT NULL DEFAULT '',
    updated_at VARCHAR(32) NOT NULL DEFAULT '',
    UNIQUE(payload_hash)
)
"""

_TABLE_READY = False


def guard_enabled() -> bool:
    """熔断开关（默认开；``GRAPE_AUTOPILOT_PAYLOAD_GUARD_ENABLED=0`` 可回滚）。"""
    raw = os.environ.get(_ENV_ENABLED, "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def dispatch_limit() -> int:
    """同一载荷允许的投递次数（默认 3；非法值回落默认，下限 1）。"""
    raw = (os.environ.get(_ENV_LIMIT) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, value)


def is_autopilot_seq(seq) -> bool:
    """event_seq 是否属于 autopilot 合成命名空间。"""
    try:
        return int(seq or 0) >= AUTOPILOT_EVENT_SEQ_BASE
    except (TypeError, ValueError):
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_table() -> bool:
    """幂等建表（首次写前调用；失败返回 False，由调用方 fail-open）。"""
    global _TABLE_READY
    if _TABLE_READY:
        return True
    try:
        with _rt_cursor_for("runtime", write=True) as c:
            c.execute(DDL)
        _TABLE_READY = True
        return True
    except Exception:  # noqa: BLE001 - 台账不可用不得阻断派发（Spec：fail-open + 告警）
        return False


def _row(payload_hash: str) -> dict | None:
    with _rt_cursor_for("runtime") as c:
        row = c.execute(
            "SELECT payload_hash, task_id, dispatch_count, failure_count, status, reason "
            "FROM " + TABLE + " WHERE payload_hash=?", (payload_hash,)).fetchone()
    if not row:
        return None
    return {"payload_hash": row[0], "task_id": int(row[1] or 0),
            "dispatch_count": int(row[2] or 0), "failure_count": int(row[3] or 0),
            "status": row[4] or STATUS_ACTIVE, "reason": row[5] or ""}


def guard_state(payload_hash: str) -> dict | None:
    """读台账状态（不存在返回 None）；查询失败返回 None（fail-open）。"""
    if not payload_hash:
        return None
    try:
        return _row(payload_hash)
    except Exception:  # noqa: BLE001
        return None


def _upsert(payload_hash: str, task_id: int, repo_key: str,
            dispatch_inc: int, failure_inc: int, limit: int) -> dict:
    """先 UPDATE 再 INSERT（`ON DUPLICATE KEY` 两后端语法不同，不用）。"""
    now = _now()
    for _attempt in range(2):
        with _rt_cursor_for("runtime", write=True) as c:
            # task_id / repo_key 只在本次调用有值时才覆盖：worker 侧的失败登记拿不到
            # autopilot task id，不能用 0 把派发侧写入的归属擦掉。
            _sets = ["dispatch_count=dispatch_count+?", "failure_count=failure_count+?",
                     "last_seen_at=?", "updated_at=?"]
            _args = [dispatch_inc, failure_inc, now, now]
            if int(task_id or 0):
                _sets.append("task_id=?")
                _args.append(int(task_id))
            if repo_key:
                _sets.append("repo_key=?")
                _args.append(repo_key)
            _args.append(payload_hash)
            cur = c.execute(
                "UPDATE " + TABLE + " SET " + ", ".join(_sets) + " WHERE payload_hash=?",
                tuple(_args))
            changed = int(getattr(cur, "rowcount", 0) or 0)
        if changed:
            break
        try:
            with _rt_cursor_for("runtime", write=True) as c:
                c.execute(
                    "INSERT INTO " + TABLE + " "
                    "(payload_hash, task_id, repo_key, dispatch_count, failure_count, "
                    "status, reason, first_seen_at, last_seen_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
                    (payload_hash, int(task_id or 0), repo_key or "",
                     dispatch_inc, failure_inc, STATUS_ACTIVE, now, now, now))
            break
        except Exception as _e:  # noqa: BLE001 - 并发插入撞 UNIQUE → 回到 UPDATE
            log.warning(f"autopilot payload guard insert raced, retry update: {_e}")
            continue
    state = _row(payload_hash) or {
        "payload_hash": payload_hash, "task_id": int(task_id or 0),
        "dispatch_count": dispatch_inc, "failure_count": failure_inc,
        "status": STATUS_ACTIVE, "reason": ""}
    # 熔断只由「同载荷失败/拒绝终态」驱动：正常周期任务即使天天同指令也不该被停
    # （Spec Risks：熔断误停正常重试）。dispatch_count 只作台账遥测，用于区分
    # 「投了很多次但都没失败」和「投几次就被拒几次」。
    over = state["failure_count"] >= limit
    reason = REASON_REPEAT_REJECTION if over else ""
    if over and state["status"] != STATUS_DEAD_LETTER:
        try:
            with _rt_cursor_for("runtime", write=True) as c:
                c.execute("UPDATE " + TABLE + " SET status=?, reason=?, updated_at=? "
                          "WHERE payload_hash=?",
                          (STATUS_DEAD_LETTER, reason, now, payload_hash))
        except Exception as _e:  # noqa: BLE001 - 台账状态写失败不影响本次裁决（fail-open）
            log.warning(f"autopilot payload guard dead-letter write failed: {_e}")
        state["status"] = STATUS_DEAD_LETTER
        state["reason"] = reason
    return state


def record_dispatch(payload_hash: str, task_id: int = 0, repo_key: str = "") -> dict:
    """登记一次投递并判定是否放行。

    返回 ``{"allowed": bool, "status": str, "reason": str, "dispatch_count": int}``；
    已是 dead_letter 的行不再累加（每 tick 一次写放大），直接拒绝。
    """
    limit = dispatch_limit()
    if not guard_enabled() or not payload_hash:
        return {"allowed": True, "status": STATUS_ACTIVE, "reason": "",
                "dispatch_count": 0}
    if not ensure_table():
        return {"allowed": True, "status": STATUS_ACTIVE, "reason": "",
                "dispatch_count": 0}
    try:
        current = _row(payload_hash)
        if current and current["status"] == STATUS_DEAD_LETTER:
            return {"allowed": False, "status": current["status"],
                    "reason": current["reason"] or REASON_REPEAT_REJECTION,
                    "dispatch_count": current["dispatch_count"]}
        state = _upsert(payload_hash, task_id, repo_key, 1, 0, limit)
        allowed = state["status"] != STATUS_DEAD_LETTER
        return {"allowed": allowed, "status": state["status"],
                "reason": "" if allowed else (state["reason"] or REASON_REPEAT_REJECTION),
                "dispatch_count": state["dispatch_count"]}
    except Exception:  # noqa: BLE001 - 台账故障 fail-open（不得阻断正常派发）
        return {"allowed": True, "status": STATUS_ACTIVE, "reason": "",
                "dispatch_count": 0}


def record_failure(payload_hash: str, task_id: int = 0, repo_key: str = "") -> dict:
    """登记一次同载荷失败/拒绝（fake completion 降级 failed 的输入）。

    返回 ``{"blocked": bool, "status": str, "reason": str, "failure_count": int}``。
    """
    limit = dispatch_limit()
    if not guard_enabled() or not payload_hash:
        return {"blocked": False, "status": STATUS_ACTIVE, "reason": "",
                "failure_count": 0}
    if not ensure_table():
        return {"blocked": False, "status": STATUS_ACTIVE, "reason": "",
                "failure_count": 0}
    try:
        current = _row(payload_hash)
        if current and current["status"] == STATUS_DEAD_LETTER:
            return {"blocked": True, "status": current["status"],
                    "reason": current["reason"] or REASON_REPEAT_REJECTION,
                    "failure_count": current["failure_count"]}
        state = _upsert(payload_hash, task_id, repo_key, 0, 1, limit)
        blocked = state["status"] == STATUS_DEAD_LETTER
        return {"blocked": blocked, "status": state["status"],
                "reason": (state["reason"] or REASON_REPEAT_REJECTION) if blocked else "",
                "failure_count": state["failure_count"]}
    except Exception:  # noqa: BLE001
        return {"blocked": False, "status": STATUS_ACTIVE, "reason": "",
                "failure_count": 0}
