"""Issue #1011 原子契约测试：派生源修正收口迁移（锚列 + 重派生回填 + 报告补写）。

覆盖（Issue 正文 In Scope 0 / AC 前三条）：
- 202611080014：agent_runtimes 锚快照列 + autopilot_tasks 姊妹字段（幂等可重跑）
- 202611080015：存量重派生回填——普通引擎重算=原值（零变更幂等两跑 uid 稳定）；
  云引擎（uid 锚成对）按新公式重派 + 引用轨道映射式改绑（池/能力/任务/消息不留孤儿）；
  对账报告落盘（tag=1011）
- 报告补写：已盖章库（版本表有行）+ 报告文件缺失 → 补写含「先前版本执行」注明；
  文件已存在不重写（幂等）；未盖章 tag 不补写
"""
import collections
import json
import os
import sqlite3

import pytest

from src.api import db_setup
from src.kernel.engine_uid import derive_engine_uid


CRED_UID = "cu-1011-0001"
CLOUD_NAME = "hermes:s11:cube:5"
PROVIDER_UID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TEMPLATE_UID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# 存量旧键（legacy 公式，含 name）派生的 uid（模拟 #868 修正前的存量行）
LEGACY_UID = derive_engine_uid(CRED_UID, CLOUD_NAME, "global", "", "")
# 新公式（uid 锚成对在场）派生的 uid
ANCHOR_UID = derive_engine_uid(CRED_UID, CLOUD_NAME, "global", "", "",
                               engine_type="hermes", provider_uid=PROVIDER_UID,
                               template_uid=TEMPLATE_UID)
# 普通引擎（无锚）uid：新旧公式同值
PLAIN_UID = derive_engine_uid(CRED_UID, "opencode", "global", "", "")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "mig.db"))
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id INTEGER DEFAULT 0,
        name TEXT NOT NULL,
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        host_name TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL,
        credential_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        credential_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE agent_type_runtime_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_type TEXT,
        runtime_id INTEGER,
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE engine_capabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        runtime_id INTEGER,
        tag TEXT,
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY,
        runtime_id INTEGER,
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE autopilot_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        engine_runtime_ids TEXT DEFAULT '',
        engine_runtime_uids TEXT DEFAULT ''
    );
    CREATE TABLE grape_t_schema_migrations (
        version TEXT PRIMARY KEY,
        name TEXT,
        kind TEXT,
        applied_at TEXT,
        duration_ms INTEGER,
        checksum TEXT
    );
    """)
    c.execute("INSERT INTO runtime_credentials (name, credential_uid) VALUES ('w1', ?)", (CRED_UID,))
    # 云引擎存量行：旧键派生 uid（含 name）+ 锚信息已在（模拟注册路径已持久化锚）
    c.execute(
        "INSERT INTO agent_runtimes (name, credential_id, engine_uid, credential_uid) "
        "VALUES (?, 1, ?, ?)", (CLOUD_NAME, LEGACY_UID, CRED_UID))
    # 普通引擎存量行
    c.execute(
        "INSERT INTO agent_runtimes (name, credential_id, engine_uid, credential_uid) "
        "VALUES ('opencode', 1, ?, ?)", (PLAIN_UID, CRED_UID))
    c.commit()
    yield c
    c.close()


def _add_anchor_columns(c):
    """跑 001 迁移（加锚快照列 + autopilot 姊妹字段）。"""
    db_setup._migration_1011_add_engine_anchor_columns(c)


# ---------------------------------------------------------------------------
# 202611080014 · 加列
# ---------------------------------------------------------------------------

class TestAddColumns:
    def test_columns_added_idempotent(self, conn):
        _add_anchor_columns(conn)
        _add_anchor_columns(conn)   # 幂等重跑
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        for col in ("engine_type", "provider_id", "template_id", "provider_uid", "template_uid"):
            assert col in cols, f"锚快照列 {col} 未建成"
        acols = [r[1] for r in conn.execute("PRAGMA table_info(autopilot_tasks)").fetchall()]
        assert "engine_runtime_uids" in acols

    def test_backfill_prereq_missing_columns_raises(self, conn):
        with pytest.raises(RuntimeError):
            db_setup._migration_1011_backfill_rederive_engine_uids(conn)


# ---------------------------------------------------------------------------
# 202611080015 · 存量重派生回填
# ---------------------------------------------------------------------------

class TestRederiveBackfill:
    def test_cloud_rederive_rebinds_references(self, conn):
        _add_anchor_columns(conn)
        # 注册路径持久化锚（uid 锚成对在场）
        conn.execute(
            "UPDATE agent_runtimes SET engine_type='hermes', provider_uid=?, template_uid=? "
            "WHERE name=?", (PROVIDER_UID, TEMPLATE_UID, CLOUD_NAME))
        # 引用轨道：池/能力/任务/消息均指向旧 uid
        conn.execute("INSERT INTO agent_type_runtime_pool (agent_type, runtime_id, engine_uid) VALUES ('coding', 1, ?)", (LEGACY_UID,))
        conn.execute("INSERT INTO engine_capabilities (runtime_id, tag, engine_uid) VALUES (1, 'all', ?)", (LEGACY_UID,))
        conn.execute("INSERT INTO agent_tasks (id, runtime_id, engine_uid) VALUES ('t1', 1, ?)", (LEGACY_UID,))
        conn.execute("INSERT INTO task_messages (task_id, engine_uid) VALUES ('t1', ?)", (LEGACY_UID,))
        conn.commit()

        report = db_setup._migration_1011_backfill_rederive_engine_uids(conn)
        assert report["engines_total"] == 2
        assert report["engines_rederived"] == 1, "仅云引擎重派（普通引擎=原值零变更）"
        assert report["engines_unchanged"] == 1
        assert conn.execute("SELECT engine_uid FROM agent_runtimes WHERE name=?", (CLOUD_NAME,)).fetchone()[0] == ANCHOR_UID
        assert conn.execute("SELECT engine_uid FROM agent_runtimes WHERE name='opencode'").fetchone()[0] == PLAIN_UID
        # 引用轨道整体改绑（映射式，旧行 uid 从行内读取），不留孤儿
        assert conn.execute("SELECT engine_uid FROM agent_type_runtime_pool").fetchone()[0] == ANCHOR_UID
        assert conn.execute("SELECT engine_uid FROM engine_capabilities").fetchone()[0] == ANCHOR_UID
        assert conn.execute("SELECT engine_uid FROM agent_tasks WHERE id='t1'").fetchone()[0] == ANCHOR_UID
        assert conn.execute("SELECT engine_uid FROM task_messages").fetchone()[0] == ANCHOR_UID

    def test_idempotent_two_runs(self, conn):
        _add_anchor_columns(conn)
        conn.execute(
            "UPDATE agent_runtimes SET engine_type='hermes', provider_uid=?, template_uid=? "
            "WHERE name=?", (PROVIDER_UID, TEMPLATE_UID, CLOUD_NAME))
        conn.commit()
        db_setup._migration_1011_backfill_rederive_engine_uids(conn)
        uids1 = conn.execute("SELECT id, engine_uid FROM agent_runtimes ORDER BY id").fetchall()
        report2 = db_setup._migration_1011_backfill_rederive_engine_uids(conn)
        uids2 = conn.execute("SELECT id, engine_uid FROM agent_runtimes ORDER BY id").fetchall()
        assert uids1 == uids2, "幂等两跑 uid 稳定"
        assert report2["engines_rederived"] == 0, "第二跑应零重派"

    def test_report_file_written(self, conn, tmp_path):
        _add_anchor_columns(conn)
        report = db_setup._migration_1011_backfill_rederive_engine_uids(conn)
        path = report.get("report_file")
        assert path and os.path.exists(path), "对账报告应落盘（tag=1011）"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["issue"] == "1011"
        assert data["report"]["engines_total"] == 2

    def test_null_uid_row_gets_derived(self, conn):
        _add_anchor_columns(conn)
        conn.execute("UPDATE agent_runtimes SET engine_uid=NULL WHERE name='opencode'")
        conn.commit()
        db_setup._migration_1011_backfill_rederive_engine_uids(conn)
        assert conn.execute("SELECT engine_uid FROM agent_runtimes WHERE name='opencode'").fetchone()[0] == PLAIN_UID


# ---------------------------------------------------------------------------
# 报告补写（已盖章存量库）
# ---------------------------------------------------------------------------

class TestReportBackfill:
    def test_stamped_db_report_backfilled_once(self, conn, tmp_path, monkeypatch):
        # 已盖章 868 迁移 + 报告文件缺失 → 补写
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name) VALUES ('202611030005', 'issue-868-session-uid-columns')")
        conn.commit()
        # 报告路径走 _uid_migration_report_path(conn)（文件库 → 库文件旁）
        out = db_setup._backfill_missing_migration_reports(conn)
        assert "868" in out, "已盖章 868 库应补写报告"
        path = out["868"]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert "先前版本" in data["note"]
        # 幂等：文件已存在不重写
        before = os.path.getmtime(path)
        out2 = db_setup._backfill_missing_migration_reports(conn)
        assert "868" not in out2
        assert os.path.getmtime(path) == before

    def test_unstamped_tag_not_backfilled(self, conn, tmp_path):
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name) VALUES ('202610300001', 'legacy-run-all-migrations')")
        conn.commit()
        out = db_setup._backfill_missing_migration_reports(conn)
        assert "1011" not in out, "1011 迁移未盖章（未跑）不补写报告"


def test_registered_versions_unique_and_cross_line_free():
    """撞号守卫回归：注册表 version 全局唯一（源内）；uuid 线五条迁移号
    不得与 weekly-10 已占用号（202611080001-003 等）重叠。"""
    versions = [m.version for m in db_setup._MIGRATIONS]
    dupes = [v for v, n in collections.Counter(versions).items() if n > 1]
    assert not dupes, f"注册表内撞号: {dupes}"
    # 2026-11-09 uuid 线并入 arch-optimization 后同注册表：
    # 202611080001-003 为同线合法注册（issue1495/1496/1497），不再是跨线撞号；
    # 跨线防撞改由「uuid 五条独占号不与其他 name 重号」+ 注册表唯一性守卫覆盖。
    names = [m.name for m in db_setup._MIGRATIONS]
    uuid_five = {"202611080011", "202611080012", "202611080013",
                 "202611080014", "202611080015"}
    assert uuid_five.issubset(set(versions)), "uuid 线五条迁移未全部注册"
    for v in uuid_five:
        idx = versions.index(v)
        assert names[idx].startswith("issue-"), f"uuid 号 {v} 被 {names[idx]} 顶替"
