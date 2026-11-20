"""探活状态机测试共用的落库/心跳助手（Issue #490 / #1982 / #2791 同族用例共用）。

原先 #490 / #1982 / #2791 三套用例各自维护一份「造引擎行 → 打公开心跳端点 → 读回状态」
的脚手架，改一份列名就要同步三处。这里上提成单份：调用方保留自己的断言与场景，
只共享「怎么造数据、怎么走线上 ingest 路径」。

约定：
- 一切状态变化都经公开心跳端点（`runtime_heartbeat` → `_heartbeat_impl`），不直写状态列，
  保证断言的是 #1982/#490 的线上时序而不是测试自己的 SQL。
- 助手只负责造数与清理，不做断言。
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock  # noqa: E402

# 摘出/恢复判定读回的列（#1982 五列版比 #2791 四列版多 probe_spawn_fail_count）
STATE_COLUMNS = ("healthy", "probe_status", "probe_fail_count", "probe_error")
STATE_COLUMNS_WITH_SPAWN = ("healthy", "probe_status", "probe_fail_count",
                            "probe_spawn_fail_count", "probe_error")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_credential(prefix: str = "cred", token_prefix: str = "h") -> int:
    """造一条 active 凭据，返回 credential_id。"""
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"{token_prefix}_{name}", name, f"dev-{name}", now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def insert_runtime(cid, name, healthy=1, probe_fail_count=0) -> int:
    """造一台 online+installed 的引擎，返回 runtime_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, scope, last_seen_at, "
            "probe_fail_count, probe_status, probe_last) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'ok', '')",
            (cid, name, healthy, now_iso(), probe_fail_count))
        rid = c.lastrowid
        db.commit()
    return rid


def cleanup(cid) -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def state(name, cid, columns=STATE_COLUMNS):
    """读回引擎状态（探活时序断言的观察面）。"""
    _cols = ", ".join(columns)
    with db_lock:
        row = db.cursor().execute(
            f"SELECT {_cols} FROM agent_runtimes WHERE name=? AND credential_id=?",
            (name, cid)).fetchone()
    return tuple(row) if row else None


class FakeRequest:
    headers = {}
    client = None


def heartbeat(probe_results, runtime_ids, cid):
    """走公开心跳端点（async 包装 → _heartbeat_impl），与线上 ingest 同一路径。"""
    from src.domains.runtime.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(runtime_ids=runtime_ids, probe_results=probe_results)
    return asyncio.run(runtime_heartbeat(body, FakeRequest(), credential_id=cid))


def observation(name, status, seq):
    """首探窗口第 seq 次观测（last_probe 各不相同 → 新观测，不被 #490 去重吃掉）。"""
    return {name: {"status": status, "latency": 0.2,
                   "last_probe": f"2026-11-17T00:00:{seq:02d}+00:00",
                   "error": "" if status == "ok" else "engine not ready"}}
