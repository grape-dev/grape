"""Issue #512: agent_runtimes 时间口径统一为 UTC（带 offset）。

背景：本机直连刷新链路 save_agent_runtimes 写 SQL 字面量 datetime('now')，
MySQL backend 经 mysql_compat 翻译为 NOW()（CST 本地墙钟、无时区），与心跳链路
UTC ISO（+00:00）同表混存，导致 last_seen_at 超前 UTC 约 8h、健康窗口误判。

本测试覆盖：
- 写入侧（save_agent_runtimes）UPDATE/INSERT 两条路径产物为 UTC ISO（带 +00:00）
- 存量迁移 migrate_agent_runtimes_utc_caliber：仅修"超前当前 UTC"的行且幂等
- 读取侧（GET /api/settings/agent-runtimes）出口 normalize_event_time 归一兜底
- 验收：online_runtime_last_seen_not_in_future（MySQL 下 last_seen_at 不超前当前 UTC）

测试环境为 SQLite（conftest 强制）；MySQL 语义通过 patch src.api.time_utils._db_backend
模拟（与 src/api/time_utils.py 的 backend 判定约定一致）。

⚠️ db/db_lock 一律在测试函数内导入（与 test_issue357_health.py / test_issue463 同约定）：
conftest 会话级 fixture 重建 session_manager.db，模块级绑定会拿到失效/分裂连接；
被测模块 src.user.db 在收集期捕获的 db 引用可能与其他模块收集期重绑的分裂
（写 A 库读 B 库），故用 _align_db 对齐到当前 session_manager 单例。
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.user import db as udb


def _align_user_db():
    """把被测模块 src.user.db 的 db/db_lock 对齐到当前 session_manager 单例。

    混跑时其他测试模块（如 test_issue_337_list_handoff）会在收集期重绑全局 db 且不恢复，
    收集期捕获引用的模块会与之分裂（写 A 库读 B 库）；先例见 test_issue463_table_governance。
    """
    import importlib
    from src.session_manager import db as _sm_db, db_lock as _sm_lock
    mod = importlib.import_module("src.user.db")
    mod.db = _sm_db
    mod.db_lock = _sm_lock


@pytest.fixture(autouse=True)
def _db_aligned():
    """对齐被测模块 db + 确保 agent_runtimes 具备代码路径所需列（幂等）。

    - _align_user_db：消除收集期其他测试模块 reload(session_manager) 造成的
      模块引用分裂（写 A 库读 B 库，先例 test_issue463_table_governance.py）；
    - org_id 列：当前代码路径（save_agent_runtimes 去重 DELETE、get_agent_runtimes
      SELECT）引用 org_id；生产由 db_setup._migrate_add_org_id_columns 幂等补齐，
      测试侧在任意被污染的测试库上同样幂等补齐（与 test_sessions.db 全量 schema 对齐）。
    """
    _align_user_db()
    from src.session_manager import db, db_lock
    from src.api.db_setup import _ensure_column
    with db_lock:
        c = db.cursor()
        # 与生产 _migrate_add_org_id_columns 同口径：agent_runtimes / runtime_credentials 均需 org_id
        _ensure_column(c, "agent_runtimes", "org_id", "INTEGER DEFAULT 0")
        _ensure_column(c, "runtime_credentials", "org_id", "INTEGER DEFAULT 0")
        db.commit()
    # 被污染/部分 schema 的测试库可能缺 user_repos（读取路由 user_db.get_user_repos 依赖），
    # 对齐后幂等补齐（与 src.user.db 模块导入时行为一致）
    udb.init_user_repos_table()
    yield


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't512_%'")
        db.commit()


def _naive_cst_now():
    """模拟 MySQL NOW()（CST 本地墙钟）写入的值：naive = UTC+8h 墙钟字符串。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _is_utc_iso(v):
    if not v:
        return False
    try:
        dt = datetime.fromisoformat(v)
        return dt.tzinfo is not None and dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0
    except Exception:
        return False


@pytest.mark.atomic
class TestSaveAgentRuntimesUtcCaliber:
    """写入侧：save_agent_runtimes 的 last_seen_at/updated_at 必须为 UTC ISO（带 +00:00）。"""

    def test_insert_path_writes_utc_iso(self):
        from src.session_manager import db, db_lock
        import src.user.db as _u
        # 维护方评审意见①：原 4 行 DEBUG print 改为断言
        assert _u.db is db, "src.user.db.db 应与 src.session_manager.db 为同一连接"
        try:
            _cols = [r[1] for r in db.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        except Exception:
            _cols = []
        assert "org_id" in _cols, f"agent_runtimes 应含 org_id 列: {_cols}"
        _clean()
        try:
            udb.save_agent_runtimes([
                {"name": "t512_utc_ins", "display_name": "T512 Ins", "installed": True,
                 "cli_path": "/usr/bin/opencode", "version": "1.0", "healthy": True},
            ])
            with db_lock:
                row = db.cursor().execute(
                    "SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_utc_ins'"
                ).fetchone()
            assert row, "INSERT 应创建本机直连行"
            for v in row:
                assert _is_utc_iso(v), f"INSERT 路径时间应为 UTC ISO 带 +00:00: {v!r}"
        finally:
            _clean()

    def test_update_path_writes_utc_iso(self):
        from src.session_manager import db, db_lock
        _clean()
        try:
            udb.save_agent_runtimes([
                {"name": "t512_utc_upd", "installed": True, "version": "v1", "healthy": True},
            ])
            udb.save_agent_runtimes([
                {"name": "t512_utc_upd", "installed": True, "version": "v2", "healthy": True},
            ])
            with db_lock:
                row = db.cursor().execute(
                    "SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_utc_upd'"
                ).fetchone()
            assert row, "UPDATE 应命中已有行"
            for v in row:
                assert _is_utc_iso(v), f"UPDATE 路径时间应为 UTC ISO 带 +00:00: {v!r}"
        finally:
            _clean()


@pytest.mark.atomic
class TestMigrateAgentRuntimesUtcCaliber:
    """存量迁移：仅修正"超前当前 UTC"的行，幂等可重放。"""

    def _seed(self):
        from src.session_manager import db, db_lock
        _clean()
        with db_lock:
            c = db.cursor()
            # 超前行：MySQL NOW() CST 墙钟（naive，按 UTC 解释落在未来 +8h）
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
                "scope, owner_id, last_seen_at, updated_at) VALUES (?,?,0,'online',1,1,'global','',?,?)",
                ("t512_future", "T512 Future", _naive_cst_now(), _naive_cst_now()))
            # 正常旧行：naive 旧时间（不超前，不应被迁移触碰）
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
                "scope, owner_id, last_seen_at, updated_at) VALUES (?,?,0,'offline',0,0,'global','','2026-01-01 10:00:00','2026-01-01 10:00:00')",
                ("t512_old", "T512 Old"))
            # 已归一的行：UTC ISO 带 offset，不应被触碰
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
                "scope, owner_id, last_seen_at, updated_at) VALUES (?,?,0,'online',1,1,'global','','2026-10-28T07:00:00+00:00','2026-10-28T07:00:00+00:00')",
                ("t512_ok", "T512 OK"))
            db.commit()

    def test_online_runtime_last_seen_not_in_future(self):
        """验收（Issue #512）：MySQL 下任何 last_seen_at 均不超前当前 UTC。

        模拟历史脏行（naive CST 落在未来 +8h）→ 迁移后所有 online 行的时间不超前当前 UTC。
        """
        from src.session_manager import db, db_lock
        self._seed()
        try:
            with patch("src.api.time_utils._db_backend", return_value="mysql"):
                udb.migrate_agent_runtimes_utc_caliber(throttle_seconds=0)
                with db_lock:
                    rows = db.cursor().execute(
                        "SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_future'"
                    ).fetchone()
            assert rows, "迁移后超前行应仍存在"
            now = datetime.now(timezone.utc)
            for v in rows:
                assert _is_utc_iso(v), f"迁移后应为 UTC ISO 带 +00:00: {v!r}"
                parsed = datetime.fromisoformat(v)
                assert parsed <= now + timedelta(seconds=60), \
                    f"last_seen_at 超前当前 UTC: {v} > {now.isoformat()}"
        finally:
            _clean()

    def test_migration_only_fixes_future_rows_and_idempotent(self):
        from src.session_manager import db, db_lock
        self._seed()
        try:
            with patch("src.api.time_utils._db_backend", return_value="mysql"):
                n1 = udb.migrate_agent_runtimes_utc_caliber(throttle_seconds=0)
                assert n1 == 1, f"仅超前行应被迁移，实际 {n1}"
                with db_lock:
                    c = db.cursor()
                    fut = c.execute("SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_future'").fetchone()
                    old = c.execute("SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_old'").fetchone()
                    ok = c.execute("SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_ok'").fetchone()
                # 非超前行原样保留
                assert old == ("2026-01-01 10:00:00", "2026-01-01 10:00:00"), old
                assert ok == ("2026-10-28T07:00:00+00:00", "2026-10-28T07:00:00+00:00"), ok
                # 再一次执行 → 不再改动（幂等）
                n2 = udb.migrate_agent_runtimes_utc_caliber(throttle_seconds=0)
                with db_lock:
                    fut2 = db.cursor().execute(
                        "SELECT last_seen_at, updated_at FROM agent_runtimes WHERE name='t512_future'").fetchone()
                assert n2 == 0, f"重复执行应无改动，实际 {n2}"
                assert fut2 == fut, f"幂等被破坏: {fut} -> {fut2}"
        finally:
            _clean()

    def test_sqlite_backend_noop(self):
        """SQLite backend 下 naive 已是 UTC，迁移应为无操作。"""
        self._seed()
        # 默认测试 backend=sqlite（conftest），不 patch
        n = udb.migrate_agent_runtimes_utc_caliber(throttle_seconds=0)
        assert n == 0, f"SQLite 后端不应迁移任何行，实际 {n}"
        _clean()

    def test_in_process_throttle_skips_rescan(self):
        """维护方评审意见②：进程内节流——同一进程两次迁移间隔低于阈值时第二次直接跳过全表扫描。"""
        from src.session_manager import db, db_lock
        self._seed()
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            udb._agent_runtimes_migrate_last_mono = 0.0
            try:
                n1 = udb.migrate_agent_runtimes_utc_caliber()
                assert n1 == 1, f"首次应执行扫描，实际 {n1}"
                # 重新制造超前行，第二次调用应因节流被跳过（不重扫）
                with db_lock:
                    db.cursor().execute(
                        "UPDATE agent_runtimes SET last_seen_at=?, updated_at=? WHERE name='t512_future'",
                        (_naive_cst_now(), _naive_cst_now()))
                    db.commit()
                n2 = udb.migrate_agent_runtimes_utc_caliber()
                assert n2 == 0, "节流窗口内应直接跳过"
                with db_lock:
                    row = db.cursor().execute(
                        "SELECT last_seen_at FROM agent_runtimes WHERE name='t512_future'").fetchone()
                assert row and not _is_utc_iso(row[0]), "节流跳过时不应发生迁移改写"
            finally:
                udb._agent_runtimes_migrate_last_mono = 0.0
                _clean()


@pytest.mark.atomic
class TestAgentRuntimesReadOutletNormalized:
    """读取侧：GET /api/settings/agent-runtimes 出口 last_seen_at/updated_at 统一归一。"""

    def test_outlet_normalizes_mysql_naive_value(self):
        from src.session_manager import db, db_lock
        _clean()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
                "scope, owner_id, last_seen_at, updated_at) VALUES (?,?,0,'online',1,1,'global','',?,?)",
                ("t512_read", "T512 Read", _naive_cst_now(), _naive_cst_now()))
            db.commit()
        from src.api.routes.settings import get_agent_runtimes
        now = datetime.now(timezone.utc)
        try:
            with patch("src.api.time_utils._db_backend", return_value="mysql"):
                d = get_agent_runtimes(user={"id": 1, "username": "u512", "auth_type": "jwt", "org_id": 0})
            rt = next((r for r in d["runtimes"] if r["name"] == "t512_read"), None)
            assert rt, f"本机引擎应可见: {[r['name'] for r in d['runtimes']]}"
            for key in ("last_seen_at", "updated_at"):
                v = rt.get(key)
                assert v is not None and v != "", f"{key} 不应为空"
                assert _is_utc_iso(v), f"{key} 应归一为 UTC ISO 带 +00:00: {v!r}"
                parsed = datetime.fromisoformat(v)
                assert parsed <= now + timedelta(seconds=60), f"{key} 超前当前 UTC: {v}"
        finally:
            _clean()