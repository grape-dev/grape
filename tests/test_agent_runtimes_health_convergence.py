"""回归测试（Issue #1496）：agent_runtimes.status 与 health 收敛（status 唯一真源）。

背景：库内遗留 health 列（现 DDL 未声明，读路径不消费）被旧写入方单独写入，
出现 health='online' AND status<>'online' 的自相矛盾行（存量 4 条 id 23259–23262），
引擎列表展示（derive_runtime_health 派生）与派发判定（engine_select._health_ok 按
status+last_seen）对同一引擎结论分叉（#758 表现的数据层根因）。

裁定（Spec 拍板）：status 为唯一真源（online/idle/offline）；health 仅允许作为
derive_runtime_health(status, last_seen_at) 的派生展示值，不参与派发。禁止
「health=online ⇒ 强抬 status」——真失联引擎会被旧 health 值反向复活。

覆盖切片：
- S1 存量回填迁移：矛盾行清零（len(bad)==0，不弱化）+ 幂等（第二次零变更）；
- S2 派生一致性：derive_runtime_health 与 status/last_seen 组合矩阵（含 stale 边界）；
- S3 写入口收敛：apply_runtime_state 同事务写 status+last_seen_at（+保留列 health）；
- S4 列表口径与派发口径同结论。
"""
import os
import sys

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import sqlite3  # noqa: E402
import time as _time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

from src.runtime.health import derive_runtime_health  # noqa: E402

_NOW = datetime.now(timezone.utc)


def _iso(dt) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# S1 · 存量回填迁移（幂等 + 矛盾清零）
# ---------------------------------------------------------------------------

def _legacy_conn() -> sqlite3.Connection:
    """存量库模拟：agent_runtimes 含 DDL 未声明的遗留 health 列 + 4 条矛盾行
    （对齐 Issue #1496 现场：id 23259–23262 均为 status=offline, health=online）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE agent_runtimes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT DEFAULT '',"
        " credential_id INTEGER DEFAULT 0,"
        " installed INTEGER DEFAULT 1,"     # Issue #1496 返工：本机口径需要
        " status TEXT DEFAULT 'offline',"
        " healthy INTEGER DEFAULT 0,"
        " last_seen_at TEXT DEFAULT NULL)")
    # 遗留列（旧版本 DDL 的痕迹，现建表语句里没有）
    conn.execute("ALTER TABLE agent_runtimes ADD COLUMN health TEXT DEFAULT ''")
    rows = [
        # (name, credential_id, status, last_seen_at, health) —— 矛盾行
        ("engine-23259", 101, "offline", _iso(_NOW - timedelta(days=3)), "online"),
        ("engine-23260", 101, "offline", _iso(_NOW - timedelta(hours=2)), "online"),
        ("engine-23261", 102, "offline", "", "online"),
        ("engine-23262", 102, "offline", None, "online"),
        # 合法行：health 已与派生口径一致（回填必须保持不动 → 幂等可验证）
        ("engine-online-fresh", 103, "online", _iso(_NOW - timedelta(seconds=10)), "online"),
        ("engine-lost", 103, "online", _iso(_NOW - timedelta(minutes=20)), "recently_lost"),
        ("engine-offline", 104, "offline", _iso(_NOW - timedelta(days=1)), "offline"),
    ]
    for name, cid, status, last_seen, health in rows:
        conn.execute(
            "INSERT INTO agent_runtimes (name, credential_id, status, last_seen_at, health)"
            " VALUES (?,?,?,?,?)", (name, cid, status, last_seen, health))
    conn.commit()
    return conn


def _bad_rows(conn: sqlite3.Connection) -> list:
    """只读矛盾 SQL（Issue #1496 验收口径）：health='online' AND status<>'online'。"""
    try:
        return conn.execute(
            "SELECT id, status, health FROM agent_runtimes"
            " WHERE health='online' AND status<>'online'").fetchall()
    except sqlite3.OperationalError:
        return []  # 无遗留 health 列的库不存在矛盾面


def _has_health_column(conn: sqlite3.Connection) -> bool:
    return any(r[1] == "health" for r in conn.execute(
        "PRAGMA table_info(agent_runtimes)").fetchall())


def _norm_fn():
    from src.api.db_setup import _fn_normalize_legacy_health_column
    return _fn_normalize_legacy_health_column


class TestLegacyHealthBackfill:
    def test_migration_registered_new_track(self):
        """迁移已注册进新轨（存量库才会执行），版本号不与已占号段冲突。"""
        from src.api import db_setup as ds
        migs = {m.version: m for m in ds._MIGRATIONS}
        assert "202611080002" in migs, "normalize_legacy_health_column 迁移未注册"
        assert migs["202611080002"].kind == "safe"
        # 撞号守卫（#1412 教训）：全库版本号唯一
        versions = [m.version for m in ds._MIGRATIONS]
        assert len(versions) == len(set(versions))

    def test_bad_rows_zero_after_backfill(self):
        """验收①：回填后只读矛盾 SQL 返回空集（len(bad)==0，不弱化）。"""
        conn = _legacy_conn()
        assert len(_bad_rows(conn)) == 4, "前置：存量矛盾行应命中 4 条"
        _norm_fn()(conn=conn)
        bad = _bad_rows(conn)
        assert len(bad) == 0, f"回填后仍剩矛盾行: {bad}"
        # 真失联引擎不得被旧 health 反向复活：offline 行回填后 health≠online
        vals = {r[0]: r[1] for r in conn.execute(
            "SELECT name, health FROM agent_runtimes").fetchall()}
        assert vals["engine-23259"] != "online"
        # status=online 且心跳新鲜的行保持 online（不误杀）
        assert vals["engine-online-fresh"] == "online"
        # status=online 但心跳超时 → recently_lost（对齐派生口径）
        assert vals["engine-lost"] == "recently_lost"

    def test_backfill_idempotent(self):
        """验收③：幂等——第二次执行零行变更。"""
        conn = _legacy_conn()
        _norm_fn()(conn=conn)
        before = conn.execute(
            "SELECT id, status, last_seen_at, health FROM agent_runtimes"
            " ORDER BY id").fetchall()
        changed = _norm_fn()(conn=conn)
        after = conn.execute(
            "SELECT id, status, last_seen_at, health FROM agent_runtimes"
            " ORDER BY id").fetchall()
        assert changed == 0, f"第二次回填仍改了 {changed} 行（幂等被破坏）"
        assert before == after

    def test_noop_without_legacy_column(self):
        """现 DDL 库（无 health 列）回填为 no-op，且不会把列加回来。"""
        conn = _legacy_conn()
        conn.execute("ALTER TABLE agent_runtimes DROP COLUMN health")
        conn.commit()
        _norm_fn()(conn=conn)
        assert not _has_health_column(conn), "回填不得引入新的 health 列"

    def test_backfill_never_trusts_stale_health_value(self):
        """回填值必须由 status+last_seen 派生，绝不信旧 health 值（防反向复活）。"""
        conn = _legacy_conn()
        # status=online 但 20min 无心跳的行：即使旧 health='online' 也必须回填为 recently_lost
        conn.execute(
            "UPDATE agent_runtimes SET health='online' WHERE name='engine-lost'")
        conn.commit()
        _norm_fn()(conn=conn)
        h = conn.execute("SELECT health FROM agent_runtimes WHERE name='engine-lost'").fetchone()[0]
        assert h == derive_runtime_health("online", _iso(_NOW - timedelta(minutes=20)), _NOW.timestamp())


# ---------------------------------------------------------------------------
# S2 · 派生一致性（口径矩阵，含 stale 边界）
# ---------------------------------------------------------------------------

class TestDerivedHealthMatrix:
    def test_matrix_matches_status_last_seen(self):
        now = _NOW.timestamp()
        fresh = _iso(_NOW - timedelta(seconds=10))
        stale = _iso(_NOW - timedelta(minutes=20))
        old = _iso(_NOW - timedelta(days=6, hours=12))
        assert derive_runtime_health("online", fresh, now) == "online"
        assert derive_runtime_health("online", stale, now) == "recently_lost"
        assert derive_runtime_health("online", None, now) == "recently_lost"
        assert derive_runtime_health("offline", fresh, now) == "recently_lost"
        assert derive_runtime_health("offline", _iso(_NOW - timedelta(days=1)), now) == "offline"
        assert derive_runtime_health("offline", old, now) == "about_to_gc"
        assert derive_runtime_health("offline", None, now) == "offline"

    def test_backfill_uses_derived_values(self):
        """回填值 == derive_runtime_health 派生值（唯一真源口径落到存量行）。"""
        conn = _legacy_conn()
        _norm_fn()(conn=conn)
        now = _time.time()
        for rid, status, last_seen, health in conn.execute(
                "SELECT id, status, last_seen_at, health FROM agent_runtimes"):
            expected = derive_runtime_health(status or "offline", last_seen, now)
            # online+新鲜 与 about_to_gc 等边界均按派生口径（宽窗口容差防执行耗时抖动）
            if expected in ("online", "recently_lost"):
                assert health in ("online", "recently_lost"), f"id={rid} {status} → {health}"
            else:
                assert health == expected, f"id={rid} {status}/{last_seen} → {health} ≠ {expected}"


# ---------------------------------------------------------------------------
# S3 · 写入口收敛（apply_runtime_state 同事务写 status+last_seen_at）
# ---------------------------------------------------------------------------

class TestApplyRuntimeState:
    def test_apply_writes_status_last_seen_same_txn(self):
        """apply_runtime_state 一次写入 status+last_seen_at（+保留列 health）。"""
        from src.runtime.health import apply_runtime_state
        conn = _legacy_conn()
        rid = conn.execute(
            "SELECT id FROM agent_runtimes WHERE name='engine-23259'").fetchone()[0]
        now = datetime.now(timezone.utc)
        with conn:
            c = conn.cursor()
            apply_runtime_state(c, rid, "online", now.isoformat())
        row = conn.execute(
            "SELECT status, last_seen_at, health FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()
        assert row[0] == "online"
        assert row[1] == now.isoformat()
        assert row[2] == "online"  # 保留列同步派生值，增量不再产生矛盾行
        # 同事务写入的行不再贡献矛盾面（其余存量行由迁移回填负责，见 S1 用例）
        assert (rid,) not in [(r[0],) for r in _bad_rows(conn)]

    def test_apply_offline_marks_health_offline(self):
        """offline + 刚刷新的 last_seen_at → 派生口径 recently_lost（对齐唯一真源，
        不再断言旧口径的 'offline'）；关键是 health 与派生值一致，无矛盾行。"""
        from src.runtime.health import apply_runtime_state
        conn = _legacy_conn()
        rid = conn.execute(
            "SELECT id FROM agent_runtimes WHERE name='engine-online-fresh'").fetchone()[0]
        now = datetime.now(timezone.utc)
        with conn:
            c = conn.cursor()
            apply_runtime_state(c, rid, "offline", now.isoformat())
        row = conn.execute(
            "SELECT status, health FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        expected = derive_runtime_health("offline", now.isoformat(), now.timestamp())
        assert row[0] == "offline"
        assert row[1] == expected  # 保留列 = 派生值（recently_lost 刚失联）
        assert rid not in [r[0] for r in _bad_rows(conn)]

    def test_reap_and_heartbeat_go_through_apply(self):
        """service.heartbeat_consolidated / reap_stale_runtimes 走统一写入口（源码级锁定）。"""
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "runtime" / "service.py"
        text = src.read_text(encoding="utf-8")
        assert "apply_runtime_state" in text, "service.py 未接入统一写入口"
        # 直接拼 status 字面量 UPDATE 的路径必须消失（防二次写窗口回潮）
        assert "SET status='online', last_seen_at=?" not in text, (
            "service.py 仍存在 status/last_seen 分离写法，应改走 apply_runtime_state")


# ---------------------------------------------------------------------------
# S4 · 列表口径与派发口径同结论
# ---------------------------------------------------------------------------

class TestListVsDispatchConvergence:
    def _member(self, status, last_seen, cid=101):
        return {"runtime_id": 1, "name": "e", "status": status,
                "healthy": True, "installed": True, "credential_id": cid,
                "last_seen_at": last_seen or ""}

    def test_same_conclusion_for_same_runtime(self):
        """同一 runtime 状态：列表 health 派生 == online ⇔ 派发 _health_ok 为真。"""
        from src.runtime.engine_select import _health_ok
        cases = [
            ("online", _iso(_NOW - timedelta(seconds=10))),   # 真在线
            ("online", _iso(_NOW - timedelta(minutes=20))),   # 心跳超时（recently_lost）
            ("online", None),                                  # 无心跳
            ("offline", _iso(_NOW - timedelta(seconds=10))),  # 矛盾面（health 派生必非 online）
            ("offline", None),
        ]
        for status, last_seen in cases:
            derived = derive_runtime_health(status, last_seen, _NOW.timestamp())
            dispatchable = _health_ok(self._member(status, last_seen))
            # 列表显示 online ⇔ 派发可用 —— 两口径对同一引擎同结论
            assert (derived == "online") == dispatchable, (
                f"口径分叉: status={status} last_seen={last_seen} "
                f"列表={derived} 派发可用={dispatchable}")

    def test_contradiction_rows_never_pass_dispatch(self):
        """存量矛盾形态（status=offline + 库内 health=online）不得被派发放行。"""
        from src.runtime.engine_select import _health_ok
        m = self._member("offline", _iso(_NOW - timedelta(seconds=5)))
        assert not _health_ok(m)


# ---------------------------------------------------------------------------
# S5 · 本机直连（cred0）派生 health 口径（Issue #1496 返工）
# ---------------------------------------------------------------------------

class TestCred0DerivedHealth:
    """本机直连引擎不参与 15s 心跳（Issue #592），派生 health 必须按
    installed+healthy 判定；此前 apply_runtime_state / 回填迁移都用远端心跳口径，
    会把 10min 探活间隔内的健康本机引擎刷成 recently_lost（本用例锁死该行为）。"""

    def _add_cred0(self, conn, *, status="online", healthy=1, installed=1,
                   seen_minutes_ago=10, health=""):
        conn.execute(
            "INSERT INTO agent_runtimes "
            "(name, credential_id, status, healthy, installed, last_seen_at, health) "
            "VALUES (?,0,?,?,?,?,?)",
            ("local-direct", status, healthy, installed,
             _iso(_NOW - timedelta(minutes=seen_minutes_ago)), health))
        conn.commit()
        return conn.execute(
            "SELECT id FROM agent_runtimes WHERE name='local-direct'").fetchone()[0]

    def test_apply_runtime_state_cred0_uses_installed_healthy(self):
        """cred0 + installed/healthy 正常 + 心跳 10min 旧 → health 仍为 online（不判失联）。"""
        from src.runtime.health import apply_runtime_state
        conn = _legacy_conn()
        rid = self._add_cred0(conn, status="offline", seen_minutes_ago=10)
        stale_seen = _iso(_NOW - timedelta(minutes=10))
        with conn:
            c = conn.cursor()
            apply_runtime_state(c, rid, "online", stale_seen)
        row = conn.execute(
            "SELECT status, last_seen_at, health FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()
        assert row[0] == "online" and row[1] == stale_seen
        assert row[2] == "online", (
            f"cred0 派生 health 应为 online（#592 口径），实际 {row[2]!r}——"
            "不能用远端心跳新鲜度判本机引擎")

    def test_apply_runtime_state_cred0_not_installed_marks_offline(self):
        """cred0 installed=0 → 派生 offline（本机口径的另一半）。"""
        from src.runtime.health import apply_runtime_state
        conn = _legacy_conn()
        rid = self._add_cred0(conn, status="online", installed=0, healthy=1)
        with conn:
            c = conn.cursor()
            apply_runtime_state(c, rid, "online", _iso(_NOW))
        h = conn.execute("SELECT health FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
        assert h == "offline", f"installed=0 的 cred0 派生应为 offline，实际 {h!r}"

    def test_backfill_cred0_uses_local_yardstick_and_stays_idempotent(self):
        """回填迁移对 cred0 同样走本机口径；且不破坏幂等（第二次零变更）。"""
        conn = _legacy_conn()
        rid = self._add_cred0(conn, status="online", healthy=1, installed=1,
                              seen_minutes_ago=10, health="recently_lost")
        _norm_fn()(conn=conn)
        h = conn.execute("SELECT health FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
        assert h == "online", f"cred0 回填应派生 online，实际 {h!r}（远端口径会误判 recently_lost）"
        assert _norm_fn()(conn=conn) == 0, "第二次回填必须零变更（幂等）"
        # 矛盾面仍为空：cred0 的 status=online/health=online 自洽
        assert len(_bad_rows(conn)) == 0
