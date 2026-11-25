"""Issue #4066：autopilot 任务 custom_fields 写侧校验（create / update）。

契约：
1. 携带保留前缀（如 `GRAPE_API_TOKEN`）→ 400，响应**回显全部冲突变量名**，且不落库；
2. 白名单变量（`GRAPE_ANALYSIS_MODE` / `GRAPE_ORG_ID` / `GRAPE_MANAGED_BY`）可保存；
3. 大写非法名（小写）同样 400，与场景保存口径一致；
4. update 未提交 custom_fields（部分更新）不被校验牵连；提交时同规则拒绝。
"""
import json
import sqlite3
import threading

import pytest
from fastapi import HTTPException

import src.domains.mgmt.routes.autopilot as ap


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "ap6066.db"))
    conn.executescript("""
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
        last_run_at TEXT DEFAULT '',
        next_run_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        engine_runtime_ids TEXT DEFAULT '',
        custom_fields TEXT DEFAULT '',
        work_branch TEXT DEFAULT '',
        engine_runtime_uids TEXT DEFAULT '',
        dispatch_state TEXT NOT NULL DEFAULT '',
        dispatch_state_reason TEXT NOT NULL DEFAULT '',
        dispatch_state_at TEXT NOT NULL DEFAULT ''
    );
    """)
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(ap, "db", conn, raising=True)
    monkeypatch.setattr(ap, "db_lock", lock, raising=True)
    # 隔离仓库/引擎归属依赖，只测 custom_fields 写侧契约
    monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_validate_engine_runtime_ids", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_validate_engine_runtime_uids", lambda *a, **k: None)
    user = {"id": 1, "username": "alice", "auth_type": "session_token"}
    return conn, user


def _create(user, fields):
    return ap.create_task(ap.CreateTaskBody(
        name="issue4066_task", instruction="巡检", custom_fields=fields), user)


def _stored(conn, task_id=None):
    sql = "SELECT id, custom_fields FROM autopilot_tasks"
    if task_id is not None:
        sql += " WHERE id=?"
        row = conn.execute(sql, (task_id,)).fetchone()
        return json.loads((row[1] if row else "") or "[]")
    return [(r[0], json.loads(r[1] or "[]")) for r in conn.execute(sql).fetchall()]


class TestCreateRejects:

    def test_reserved_field_400_and_echoes_names(self, env):
        conn, user = env
        with pytest.raises(HTTPException) as ei:
            _create(user, [
                {"name": "GRAPE_API_TOKEN", "value": "x"},
                {"name": "TEAM_FLAG", "value": "1"},
                {"name": "GRAPE_WORK_BRANCH", "value": "dev"},
            ])
        exc = ei.value
        assert exc.status_code == 400
        detail = exc.detail
        msg = detail.get("message", "")
        assert "GRAPE_API_TOKEN" in msg and "GRAPE_WORK_BRANCH" in msg, msg
        assert "TEAM_FLAG" not in detail.get("invalid_names", []), \
            "合法变量不应出现在冲突名列表"
        assert set(detail.get("invalid_names") or []) == {"GRAPE_API_TOKEN", "GRAPE_WORK_BRANCH"}

    def test_reserved_field_not_persisted(self, env):
        _, user = env
        with pytest.raises(HTTPException):
            _create(user, [{"name": "GRAPE_SESSION_TOKEN", "value": "tk_leak"}])
        assert _stored(env[0]) == []

    def test_lowercase_name_400(self, env):
        _, user = env
        with pytest.raises(HTTPException) as ei:
            _create(user, [{"name": "team_flag", "value": "1"}])
        assert ei.value.status_code == 400
        assert "大写" in ei.value.detail.get("message", "")

    def test_non_dict_entries_do_not_block_creation(self, env):
        """校验只按 name 判定：非 dict 脏条目不应让整次保存 400。"""
        conn, user = env
        resp = _create(user, ["garbage", {"name": "TEAM_FLAG", "value": "1"}])
        assert resp["id"]
        stored = _stored(conn, resp["id"])
        assert {"name": "TEAM_FLAG", "value": "1"} in stored


class TestCreateAccepts:

    @pytest.mark.parametrize("fields", [
        [{"name": "GRAPE_ANALYSIS_MODE", "value": "all"}],
        [{"name": "GRAPE_ORG_ID", "value": "42"}],
        [{"name": "GRAPE_MANAGED_BY", "value": "org-analysis"}],
        [
            {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
            {"name": "GRAPE_ORG_ID", "value": "42"},
            {"name": "GRAPE_MANAGED_BY", "value": "org-analysis"},
            {"name": "TEAM_FLAG", "value": "1"},
        ],
    ])
    def test_whitelist_and_plain_fields_saved(self, env, fields):
        conn, user = env
        resp = _create(user, fields)
        assert _stored(conn, resp["id"]) == fields

    def test_no_custom_fields_ok(self, env):
        conn, user = env
        resp = _create(user, [])
        assert _stored(conn, resp["id"]) == []


class TestUpdate:

    def _seed(self, conn):
        conn.execute(
            "INSERT INTO autopilot_tasks (id, name, custom_fields) VALUES (9, 't', ?)",
            (json.dumps([{"name": "TEAM_FLAG", "value": "keep"}]),))
        conn.commit()

    def test_update_reserved_field_400_and_not_persisted(self, env, monkeypatch):
        conn, user = env
        self._seed(conn)
        monkeypatch.setattr(ap, "_check_task_access", lambda u, tid: {"id": tid, "repo_key": ""})
        with pytest.raises(HTTPException) as ei:
            ap.update_task(9, ap.UpdateTaskBody(
                custom_fields=[{"name": "GRAPE_API_TOKEN", "value": "x"}]), user)
        assert ei.value.status_code == 400
        assert "GRAPE_API_TOKEN" in ei.value.detail.get("message", "")
        assert _stored(conn, 9) == [{"name": "TEAM_FLAG", "value": "keep"}]

    def test_update_whitelist_field_saved(self, env, monkeypatch):
        conn, user = env
        self._seed(conn)
        monkeypatch.setattr(ap, "_check_task_access", lambda u, tid: {"id": tid, "repo_key": ""})
        fields = [{"name": "GRAPE_ORG_ID", "value": "7"},
                  {"name": "TEAM_FLAG", "value": "keep"}]
        ap.update_task(9, ap.UpdateTaskBody(custom_fields=fields), user)
        assert _stored(conn, 9) == fields

    def test_update_without_custom_fields_untouched(self, env, monkeypatch):
        conn, user = env
        self._seed(conn)
        monkeypatch.setattr(ap, "_check_task_access", lambda u, tid: {"id": tid, "repo_key": ""})
        ap.update_task(9, ap.UpdateTaskBody(name="renamed"), user)
        assert _stored(conn, 9) == [{"name": "TEAM_FLAG", "value": "keep"}]