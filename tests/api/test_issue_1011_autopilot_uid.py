"""Issue #1011 原子契约测试：autopilot 引擎绑定姊妹字段（uid-first）。

覆盖（Issue 正文 In Scope 2 / UC-G/H）：
- H1 保存带 engine_runtime_uids：uid 校验归属 fail-fast；升格语义（写 uids 清空 ids）；
  旧 payload 只带 ids 兼容解析（legacy 模式）
- 双非空拒绝：uids 与 ids 同时非空 → 400
- 读侧：engine_runtime_uids 非空优先，空回落 engine_runtime_ids（legacy 只读）
- H2 升格脚本：有效 id 补 uids 幂等两跑 uid 稳定；悬空 id 跳过 + 报告不静默
- UC-G 入队三分支判据（scheduler 任务 dict 的 uid 模式 → _engines_by_uids 健康过滤）
- _engines_by_uids：uid 命中/未命中/缺列降级
"""
import json
import os
import sqlite3
import threading

import pytest
from fastapi import HTTPException

import src.domains.mgmt.routes.autopilot as ap
import src.domains.mgmt.user.db as udb
from src.api.engine_uid import derive_engine_uid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


UID_A = derive_engine_uid("", "i1011-a", "global", "", "")
UID_B = derive_engine_uid("", "i1011-b", "global", "", "")
UID_STALE = "deadbeef-dead-5dea-8dea-deaddeaddead"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "ap.db"))
    conn.executescript("""
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        device_name TEXT DEFAULT '',
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        repo_key TEXT DEFAULT '',
        last_seen_at TEXT DEFAULT '',
        last_ip TEXT DEFAULT '',
        org_id INTEGER DEFAULT 0,
        spec TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        credential_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0,
        installed INTEGER DEFAULT 1,
        last_seen_at TEXT DEFAULT NULL,
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        role_tags TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE autopilot_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT 'issue',
        instruction TEXT DEFAULT '',
        skill_name TEXT DEFAULT '',
        cron TEXT DEFAULT '0 9 * * *',
        max_retry INTEGER DEFAULT 3,
        enabled INTEGER DEFAULT 1,
        next_run_at TEXT DEFAULT '',
        last_run_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        engine_runtime_ids TEXT DEFAULT '',
        custom_fields TEXT DEFAULT '',
        work_branch TEXT DEFAULT '',
        engine_runtime_uids TEXT DEFAULT ''
    );
    """)
    # 凭据 3（global, alice）→ 引擎 1（uid A）；凭据 3 → 引擎 2（uid B）；悬空 id=77
    conn.execute("INSERT INTO runtime_credentials (name, scope, owner_name) VALUES ('i1011_glob', 'global', 'alice')")
    conn.execute("INSERT INTO agent_runtimes (name, credential_id, engine_uid, status, healthy, installed, last_seen_at, scope) VALUES ('i1011-a', 3, ?, 'online', 1, 1, '2026-01-01T00:00:00+00:00', 'global')", (UID_A,))
    conn.execute("INSERT INTO agent_runtimes (name, credential_id, engine_uid, status, healthy, installed, last_seen_at, scope) VALUES ('i1011-b', 3, ?, 'online', 1, 1, '2026-01-01T00:00:00+00:00', 'global')", (UID_B,))
    conn.execute("UPDATE agent_runtimes SET id=3 WHERE engine_uid=?", (UID_B,)) if False else None
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(ap, "db", conn, raising=True)
    monkeypatch.setattr(ap, "db_lock", lock, raising=True)
    monkeypatch.setattr(udb, "db", conn, raising=True)
    monkeypatch.setattr(udb, "db_lock", lock, raising=True)
    user = {"id": 0, "username": "alice", "auth_type": "session_token"}
    return conn, user


def _rids(conn):
    return {r[1]: r[0] for r in conn.execute("SELECT id, name FROM agent_runtimes").fetchall()}


# ---------------------------------------------------------------------------
# H1 · 校验 + 升格语义 + 双非空拒绝
# ---------------------------------------------------------------------------

class TestAutopilotUidValidation:
    def test_validate_uids_ok(self, env):
        conn, user = env
        ap._validate_engine_runtime_uids(user, "", [UID_A, UID_B])   # 不抛 = 通过

    def test_validate_uids_stale_400(self, env):
        conn, user = env
        with pytest.raises(HTTPException) as ei:
            ap._validate_engine_runtime_uids(user, "", [UID_STALE])
        assert ei.value.status_code == 400

    def test_validate_uids_invalid_form_400(self, env):
        conn, user = env
        with pytest.raises(HTTPException) as ei:
            ap._validate_engine_runtime_uids(user, "", ["not-a-uuid"])
        assert ei.value.status_code == 400

    def test_dual_nonempty_rejected(self, env):
        with pytest.raises(HTTPException) as ei:
            ap._reject_dual_engine_fields([UID_A], [1])
        assert ei.value.status_code == 400

    def test_dual_not_triggered_when_one_empty(self, env):
        ap._reject_dual_engine_fields([], [1])
        ap._reject_dual_engine_fields([UID_A], [])


class TestCreateUpdate:
    def test_create_uid_mode_upgrades(self, env, monkeypatch):
        conn, user = env
        monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None)
        body = ap.CreateTaskBody(name="i1011_task", instruction="x",
                                 engine_runtime_uids=[UID_A, UID_B])
        resp = ap.create_task(body, user)
        row = conn.execute("SELECT engine_runtime_uids, engine_runtime_ids FROM autopilot_tasks WHERE id=?", (resp["id"],)).fetchone()
        assert json.loads(row[0]) == [UID_A, UID_B]
        assert row[1] == "", "升格语义：写 uids 必须清空 ids"

    def test_create_legacy_ids_mode(self, env, monkeypatch):
        conn, user = env
        monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None)
        rids = _rids(conn)
        body = ap.CreateTaskBody(name="i1011_task", instruction="x",
                                 engine_runtime_ids=[rids["i1011-a"]])
        resp = ap.create_task(body, user)
        row = conn.execute("SELECT engine_runtime_uids, engine_runtime_ids FROM autopilot_tasks WHERE id=?", (resp["id"],)).fetchone()
        assert row[0] == "" and json.loads(row[1]) == [rids["i1011-a"]]

    def test_create_dual_nonempty_400(self, env, monkeypatch):
        conn, user = env
        monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None)
        rids = _rids(conn)
        body = ap.CreateTaskBody(name="i1011_task", instruction="x",
                                 engine_runtime_uids=[UID_A], engine_runtime_ids=[rids["i1011-a"]])
        with pytest.raises(HTTPException) as ei:
            ap.create_task(body, user)
        assert ei.value.status_code == 400

    def test_update_uid_mode_clears_ids(self, env, monkeypatch):
        conn, user = env
        monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None)
        monkeypatch.setattr(ap, "_check_task_access", lambda user, tid: {"id": tid, "repo_key": ""})
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) VALUES (5, 't', '[1,2]')")
        conn.commit()
        body = ap.UpdateTaskBody(engine_runtime_uids=[UID_A])
        ap.update_task(5, body, user)
        row = conn.execute("SELECT engine_runtime_uids, engine_runtime_ids FROM autopilot_tasks WHERE id=5").fetchone()
        assert json.loads(row[0]) == [UID_A]
        assert row[1] == "", "更新升格后 ids 应被清空"

    def test_row_to_task_uids_first(self, env):
        conn, _ = env
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_uids, engine_runtime_ids) "
                     "VALUES (7, 't', ?, '[9]')", (json.dumps([UID_A]),))
        conn.commit()
        row = conn.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, "
            "enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
            "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids "
            "FROM autopilot_tasks WHERE id=7").fetchone()
        task = ap._row_to_task(row)
        assert task["engine_runtime_uids"] == [UID_A]
        assert task["engine_runtime_ids"] == [9]

    def test_row_to_task_legacy_fallback(self, env):
        conn, _ = env
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) VALUES (8, 't', '[9]')")
        conn.commit()
        row = conn.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, "
            "enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
            "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids "
            "FROM autopilot_tasks WHERE id=8").fetchone()
        task = ap._row_to_task(row)
        assert task["engine_runtime_uids"] == []
        assert task["engine_runtime_ids"] == [9], "旧数据（uids 空）回落 ids 不报错"


# ---------------------------------------------------------------------------
# UC-G · 引擎解析（uid 池路径）+ 入队三分支判据
# ---------------------------------------------------------------------------

class TestEnginesByUids:
    def test_hit_and_miss(self, env, monkeypatch):
        conn, _ = env
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_db", lambda: (conn, threading.RLock()))
        from src.domains.runtime.engine.engine_select import _engines_by_uids
        members = _engines_by_uids([UID_A, UID_STALE], "")
        assert [m["runtime_id"] for m in members] and len(members) == 1
        assert members[0]["engine_uid"] == UID_A

    def test_missing_column_degrades(self, env, monkeypatch):
        conn, _ = env
        conn.execute("ALTER TABLE agent_runtimes RENAME TO ar_backup")
        conn.execute("CREATE TABLE agent_runtimes (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_db", lambda: (conn, threading.RLock()))
        from src.domains.runtime.engine.engine_select import _engines_by_uids
        assert _engines_by_uids([UID_A], "") == []


class TestSchedulerUidPoolPath:
    """回归（部署实测抓出）：scheduler uid 模式池解析调用 _norm_repo 但未导入，
    NameError 被吞成 parse 失败 → _pool_engine_ids 落空 → 绑定任务退化为广播。

    守卫：本地 import 必须携带 _norm_repo，且 resolve(uid,_norm_repo(repo)) 路径可用。
    """

    def test_scheduler_uid_mode_import_includes_norm_repo(self):
        import ast
        src = open(
            os.path.join(ROOT, "src", "domains", "runtime", "engine", "scheduler.py"),
            encoding="utf-8").read()
        tree = ast.parse(src)
        uid_import = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and node.module == "src.domains.runtime.engine.engine_select"):
                for a in node.names:
                    if a.name == "_engines_by_uids":
                        uid_import = [x.name for x in node.names]
        assert uid_import is not None, "uid 模式本地 import 未找到"
        assert "_norm_repo" in uid_import, \
            f"uid 模式本地 import 缺 _norm_repo（NameError → 绑定任务退化为广播）: {uid_import}"

    def test_scheduler_uid_mode_resolves_healthy_pool(self, env, monkeypatch):
        """uid 模式：读取(uid, repo) 走 _engines_by_uids + _health_ok 健康过滤（行语义）。"""
        conn, _ = env
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE agent_runtimes SET last_seen_at=?, status='online', healthy=1 "
                     "WHERE engine_uid IN (?, ?)", (now, UID_A, UID_B))
        conn.commit()
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_db", lambda: (conn, threading.RLock()))
        from src.domains.runtime.engine.engine_select import _engines_by_uids, _health_ok, _norm_repo
        # 引擎 A healthy；引擎 B healthy；STALE 不命中 —— 与 #1011 UC-G 契约一致
        members = _engines_by_uids([UID_A, UID_B, UID_STALE], _norm_repo("gitcode:org/i1011"))
        healthy = [m["runtime_id"] for m in members if _health_ok(m)]
        assert 1 in healthy and 2 in healthy and 3 not in healthy, healthy


# ---------------------------------------------------------------------------
# H2 · 存量升格脚本（幂等两跑 uid 稳定 / 悬空 id 报告不静默）
# ---------------------------------------------------------------------------

class TestUpgradeScript:
    def test_upgrade_idempotent_and_dangling_reported(self, env, tmp_path, monkeypatch):
        conn, _ = env
        rids = _rids(conn)
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        # 任务 1：有效 ids；任务 2：悬空 id；任务 3：纯配置（空 ids）
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) "
                     "VALUES (1, 't1', ?)", (json.dumps([rids["i1011-a"], rids["i1011-b"]]),))
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) "
                     "VALUES (2, 't2', ?)", (json.dumps([77]),))
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) VALUES (3, 't3', '')")
        conn.commit()
        result = up.scan()
        assert [it["task_id"] for it in result["upgradable"]] == [1]
        assert [it["task_id"] for it in result["dangling"]] == [2]
        assert result["dangling"][0]["dangling_ids"] == [77]
        n = up.apply(result["upgradable"])
        assert n == 1
        row = conn.execute("SELECT engine_runtime_uids, engine_runtime_ids FROM autopilot_tasks WHERE id=1").fetchone()
        assert json.loads(row[0]) == [UID_A, UID_B]
        assert row[1] == ""
        # 幂等两跑：uids 已非空 → 0 upgradable，uid 稳定
        result2 = up.scan()
        assert result2["upgradable"] == []
        assert result2["already_uid"] >= 1
        row2 = conn.execute("SELECT engine_runtime_uids FROM autopilot_tasks WHERE id=1").fetchone()
        assert json.loads(row2[0]) == [UID_A, UID_B]
        # 悬空任务不被升格（不静默）
        row3 = conn.execute("SELECT engine_runtime_ids FROM autopilot_tasks WHERE id=2").fetchone()
        assert json.loads(row3[0]) == [77]


# ---------------------------------------------------------------------------
# H2-v2 · 悬空 id 历史反查（agent_tasks pool_engine_ids ↔ pool_engine_uids 对齐）
# ---------------------------------------------------------------------------

UID_HIST = "a1a1a1a1-1111-4111-8111-a1a1a1a1a1a1"
UID_HIST2 = "b2b2b2b2-2222-4222-8222-b2b2b2b2b2b2"


@pytest.fixture()
def hist_env(env):
    """在 env 之上加 agent_tasks 历史表（pool 双写列）。"""
    conn, user = env
    conn.executescript("""
    CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool_engine_ids TEXT DEFAULT '',
        pool_engine_uids TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL,
        engine_name_snapshot TEXT DEFAULT '',
        status TEXT DEFAULT 'queued'
    );
    """)
    conn.commit()
    return conn, user


class TestUpgradeScriptRecover:
    def _task(self, conn, tid, ids):
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) VALUES (?,?,?)",
                     (tid, f"t{tid}", ids))
        conn.commit()

    def test_recovered_single_history_upgrades(self, hist_env, tmp_path, monkeypatch):
        """悬空 id=77 有唯一历史 uid → 反查成功并入 upgradable（source=history）。"""
        conn, _ = hist_env
        self._task(conn, 11, "[77]")
        conn.execute("INSERT INTO agent_tasks (pool_engine_ids, pool_engine_uids) VALUES (?,?)",
                     ("[77,88]", json.dumps([UID_HIST, UID_HIST2])))
        conn.commit()
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        result = up.scan()
        hit = [it for it in result["upgradable"] if it["task_id"] == 11]
        assert hit and hit[0]["source"] == "history"
        assert hit[0]["uids"] == [UID_HIST]
        n = up.apply(result["upgradable"])
        assert n == 1
        row = conn.execute("SELECT engine_runtime_uids, engine_runtime_ids "
                           "FROM autopilot_tasks WHERE id=11").fetchone()
        assert json.loads(row[0]) == [UID_HIST]
        assert row[1] == ""

    def test_misaligned_history_row_skipped(self, hist_env, tmp_path, monkeypatch):
        """历史行 ids/uids 长度不匹配（双写空窗坏行）→ 该行不可信，任务保持 dangling。"""
        conn, _ = hist_env
        self._task(conn, 12, "[77]")
        conn.execute("INSERT INTO agent_tasks (pool_engine_ids, pool_engine_uids) VALUES (?,?)",
                     ("[77,88]", json.dumps([UID_HIST])))  # 长度 2 vs 1
        conn.commit()
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        result = up.scan()
        assert [it["task_id"] for it in result["dangling"]] == [12]
        assert result["dangling"][0]["unrecovered"][0]["reason"] == "history_missing"

    def test_conflicting_history_keeps_dangling(self, hist_env, tmp_path, monkeypatch):
        """同一悬空 id 在历史中映射到多个 uid → 冲突，不做升格（部分升格禁止）。"""
        conn, _ = hist_env
        self._task(conn, 13, "[77]")
        conn.execute("INSERT INTO agent_tasks (pool_engine_ids, pool_engine_uids) VALUES (?,?)",
                     ("[77]", json.dumps([UID_HIST])))
        conn.execute("INSERT INTO agent_tasks (pool_engine_ids, pool_engine_uids) VALUES (?,?)",
                     ("[77]", json.dumps([UID_HIST2])))
        conn.commit()
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        result = up.scan()
        assert [it["task_id"] for it in result["dangling"]] == [13]
        assert result["dangling"][0]["unrecovered"][0]["reason"] == "conflict"
        row = conn.execute("SELECT engine_runtime_ids FROM autopilot_tasks WHERE id=13").fetchone()
        assert json.loads(row[0]) == [77], "冲突任务不升格不写行"

    def test_mixed_task_no_partial_upgrade(self, hist_env, tmp_path, monkeypatch):
        """混合任务（存活 id + 可恢复悬空 id + 不可恢复悬空 id）→ 整体 dangling 不部分升格。"""
        conn, _ = hist_env
        rids = _rids(conn)
        live_id = rids["i1011-a"]
        conn.execute("INSERT INTO autopilot_tasks (id, name, engine_runtime_ids) VALUES (14, 't14', ?)",
                     (json.dumps([live_id, 77, 99]),))
        conn.execute("INSERT INTO agent_tasks (pool_engine_ids, pool_engine_uids) VALUES (?,?)",
                     ("[77]", json.dumps([UID_HIST])))  # 77 可恢复；99 无历史
        conn.commit()
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        result = up.scan()
        assert [it["task_id"] for it in result["dangling"]] == [14]
        d = result["dangling"][0]
        assert d["recover"] == "partial" and d["recovered"] == {77: UID_HIST}
        assert d["unrecovered"][0]["id"] == 99
        row = conn.execute("SELECT engine_runtime_ids, engine_runtime_uids "
                           "FROM autopilot_tasks WHERE id=14").fetchone()
        assert json.loads(row[0]) == [live_id, 77, 99] and row[1] == "", "ids 原样保留"

    def test_missing_agent_tasks_table_degrades(self, env, tmp_path, monkeypatch):
        """无 agent_tasks 表（未迁移旧库）→ 容错降级 None，悬空照旧报告不崩溃。"""
        conn, _ = env
        self._task(conn, 15, "[77]")
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "ap.db"))
        import scripts.upgrade_autopilot_engine_uids as up
        monkeypatch.setattr(up, "db", conn, raising=True)
        monkeypatch.setattr(up, "db_lock", threading.RLock(), raising=True)
        result = up.scan()   # 不抛 = 降级成立
        assert [it["task_id"] for it in result["dangling"]] == [15]
        assert result["dangling"][0]["recover"] == "no_history"
