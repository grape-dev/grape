"""Issue #868 原子契约测试（Scope 修订追加）：前端 uid 消费。

口径（评论 id=198094644，与后端 T10 会话表双读一致）：前端对引擎/会话的引用与展示
改为 engine_uid 优先、自增 id 兜底。可测子集（真实 SQLite 临时库，不连 MySQL）：

- F1 GET /chat/config：backends / selectedEngine 带 engine_uid（uid 优先、id 兜底；
     旧库缺 engine_uid 列降级 ''，引擎列表不塌）
- F2 PUT /chat/config：接受 engine_uid，uid 有效可解析 → 覆盖 runtime_id（uid 优先）；
     uid 无效/失效/缺列 → 回退 body.runtime_id（旧数据不报错）
- F3 obs trace _task_item：任务项带 engine_uid（缺列/行不存在降级 ''）
- F4 get_detached_engine_hints：解绑提示带 engine_uid（缺列降级 ''）
"""
import asyncio
import importlib
import sqlite3
import threading

import pytest

from src.api.engine_uid import derive_engine_uid


UID_HERMES = derive_engine_uid("", "hermes", "global", "", "")
UID_OPENCODE = derive_engine_uid("", "opencode", "global", "", "")

_RUNTIMES_UID = """
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    version TEXT DEFAULT '',
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    host_name TEXT DEFAULT '',
    status TEXT DEFAULT 'online',
    healthy INTEGER DEFAULT 1,
    installed INTEGER DEFAULT 1,
    engine_uid VARCHAR(36) DEFAULT ''
)
"""

# 旧库（迁移未跑）：无 engine_uid 列
_RUNTIMES_LEGACY = _RUNTIMES_UID.replace("    engine_uid VARCHAR(36) DEFAULT ''\n", "").replace(
    "installed INTEGER DEFAULT 1,", "installed INTEGER DEFAULT 1")

_RUNTIME_CREDS = """
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    device_name TEXT DEFAULT '',
    scope TEXT DEFAULT 'user',
    owner_id TEXT DEFAULT '',
    owner_name TEXT DEFAULT '',
    status TEXT DEFAULT 'active'
)
"""

_CHAT_SESSION_UID = """
CREATE TABLE chat_agent_session (
    id TEXT PRIMARY KEY,
    user_id INTEGER DEFAULT 0,
    repo_key TEXT DEFAULT '',
    runtime_id INTEGER DEFAULT 0,
    engine_uid VARCHAR(36) DEFAULT '',
    skill TEXT DEFAULT '',
    skill_id INTEGER DEFAULT 0,
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    im_provider TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    created_at TEXT DEFAULT '',
    last_active_at TEXT DEFAULT ''
)
"""

_DETACH_LOG = """
CREATE TABLE scene_engine_detach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_row_id INTEGER DEFAULT 0,
    scene_id TEXT DEFAULT '',
    node_tag TEXT DEFAULT '',
    runtime_id INTEGER DEFAULT 0,
    runtime_name TEXT DEFAULT '',
    detached_at TEXT DEFAULT '',
    rebound_at TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    affected_autopilot_tasks TEXT DEFAULT ''
)
"""


def _db_with_runtimes(uid: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_RUNTIMES_UID if uid else _RUNTIMES_LEGACY)
    conn.execute(_RUNTIME_CREDS)
    # 本机直连 opencode（credential_id=0）+ 个人 hermes（scope=user）
    if uid:
        conn.execute("INSERT INTO agent_runtimes (credential_id, name, version, status, healthy, installed, engine_uid) "
                     "VALUES (0, 'opencode', '1.0', 'online', 1, 1, ?)", (UID_OPENCODE,))
        conn.execute("INSERT INTO agent_runtimes (credential_id, name, version, scope, owner_id, status, healthy, installed, engine_uid) "
                     "VALUES (1, 'hermes', '1.0', 'user', '7', 'online', 1, 1, ?)", (UID_HERMES,))
    else:
        conn.execute("INSERT INTO agent_runtimes (credential_id, name, version, status, healthy, installed) "
                     "VALUES (0, 'opencode', '1.0', 'online', 1, 1)")
        conn.execute("INSERT INTO agent_runtimes (credential_id, name, version, scope, owner_id, status, healthy, installed) "
                     "VALUES (1, 'hermes', '1.0', 'user', '7', 'online', 1, 1)")
    conn.execute("INSERT INTO runtime_credentials (name, device_name, scope, owner_id, owner_name, status) "
                 "VALUES ('dev1', 'dev-1', 'user', '7', 'alice', 'active')")
    conn.commit()
    return conn


def _patch_db(monkeypatch, conn):
    """把 src.session_manager.db/db_lock 指到临时库（chat.py / trace.py / engine_select 均从此导入）。"""
    sm = importlib.import_module("src.session_manager")
    lock = threading.RLock()
    monkeypatch.setattr(sm, "db", conn, raising=True)
    monkeypatch.setattr(sm, "db_lock", lock, raising=True)
    return sm


# ---------- F1 GET /chat/config ----------

class TestChatConfigGetUid:
    def test_backends_and_selected_engine_carry_uid(self, tmp_path, monkeypatch):
        conn = _db_with_runtimes(uid=True)
        conn.execute(_CHAT_SESSION_UID)
        conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                     "VALUES ('sess-cfg-1', 1, '', 1, ?)", (UID_OPENCODE,))
        conn.commit()
        _patch_db(monkeypatch, conn)
        chat = importlib.import_module("src.domains.runtime.routes.chat")
        d = chat.get_chat_config(user={"id": 1, "username": "alice"}, repo="")
        backs = d.get("_backends") or []
        assert backs, "引擎列表不应为空"
        by_name = {b["name"]: b for b in backs}
        assert by_name["opencode"].get("engine_uid") == UID_OPENCODE
        _hermes = next(b for b in backs if b["scope"] == "user")
        assert _hermes.get("engine_uid") == UID_HERMES
        sel = d.get("selectedEngine") or {}
        # 指针命中候选 → uid 随候选返回
        assert sel.get("engine_uid") == UID_OPENCODE

    def test_selected_engine_pointer_outside_candidates_carries_uid(self, tmp_path, monkeypatch):
        conn = _db_with_runtimes(uid=True)
        conn.execute(_CHAT_SESSION_UID)
        # 指针指向候选外引擎（id=2，不在 chat 可选面）→ 反查路径也要带 uid
        conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                     "VALUES ('sess-cfg-1', 1, '', 2, ?)", (UID_HERMES,))
        conn.commit()
        _patch_db(monkeypatch, conn)
        chat = importlib.import_module("src.domains.runtime.routes.chat")
        cfg = {"runtime_id": 2, "backend": "hermes"}
        sel = chat._chat_selected_engine(cfg, [])
        assert sel and sel.get("runtime_id") == 2
        assert sel.get("engine_uid") == UID_HERMES

    def test_legacy_db_missing_uid_column_degrades(self, tmp_path, monkeypatch):
        conn = _db_with_runtimes(uid=False)
        conn.execute(_CHAT_SESSION_UID.replace("    engine_uid VARCHAR(36) DEFAULT '',\n", ""))
        conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id) "
                     "VALUES ('sess-cfg-1', 1, '', 1)")
        conn.commit()
        _patch_db(monkeypatch, conn)
        chat = importlib.import_module("src.domains.runtime.routes.chat")
        d = chat.get_chat_config(user={"id": 1, "username": "alice"}, repo="")
        backs = d.get("_backends") or []
        assert backs, "旧库缺 uid 列：引擎列表不塌"
        assert all((b.get("engine_uid") or "") == "" for b in backs)
        sel = d.get("selectedEngine") or {}
        assert sel.get("runtime_id") == 1 and sel.get("engine_uid") == ""


# ---------- F2 PUT /chat/config ----------

class TestChatConfigPutUid:
    def _run(self, monkeypatch, body, uid_to_rid):
        chat = importlib.import_module("src.domains.runtime.routes.chat")
        udb = importlib.import_module("src.domains.mgmt.user.db")
        monkeypatch.setattr(chat, "_get_chat_config", lambda uid: {
            "backend": "x", "runtime_id": 7, "skill": "", "skill_id": 0,
            "im_app_id": "", "im_app_secret": "", "im_provider": "", "engine_uid": "",
        }, raising=True)
        captured = {}

        def _set(uid, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(chat, "_set_chat_config", _set, raising=True)
        monkeypatch.setattr(udb, "get_runtime_id_by_engine_uid", lambda u: uid_to_rid.get(u), raising=True)
        from types import SimpleNamespace as _NS
        _req = _NS(client=_NS(host="127.0.0.1"), headers={}, query_params={})
        asyncio.run(chat.update_chat_config(body=body, request=_req, user={"id": 1, "username": "alice"}, repo=""))
        return captured

    def test_valid_uid_overrides_runtime_id(self, monkeypatch):
        captured = self._run(monkeypatch, {"backend": "x", "engine_uid": UID_OPENCODE, "runtime_id": 7},
                             {UID_OPENCODE: 9})
        assert captured.get("runtime_id") == 9, "uid 有效可解析 → uid 优先覆盖 runtime_id"

    def test_invalid_uid_falls_back_to_runtime_id(self, monkeypatch):
        captured = self._run(monkeypatch, {"backend": "x", "engine_uid": "not-a-uid", "runtime_id": 7}, {})
        assert captured.get("runtime_id") == 7, "uid 非法 → 回退 body.runtime_id"

    def test_stale_uid_falls_back_to_runtime_id(self, monkeypatch):
        captured = self._run(monkeypatch, {"backend": "x", "engine_uid": UID_HERMES, "runtime_id": 7},
                             {UID_HERMES: None})
        assert captured.get("runtime_id") == 7, "uid 失效（不可解析）→ 回退 body.runtime_id"

    def test_uid_only_no_fallback_keeps_backend_resolution(self, monkeypatch):
        # 前端只传 uid（旧字段 runtime_id 缺失）且 uid 失效 → 不写 runtime_id，走 backend 名解析
        captured = self._run(monkeypatch, {"backend": "x", "engine_uid": UID_HERMES}, {UID_HERMES: None})
        assert "runtime_id" not in captured


# ---------- F3 obs trace 任务项 ----------

class TestTraceTaskItemUid:
    def test_task_item_carries_engine_uid(self, monkeypatch):
        conn = _db_with_runtimes(uid=True)
        _patch_db(monkeypatch, conn)
        trace = importlib.import_module("src.domains.insight.observability.trace")
        item = trace._task_item({"id": "t1", "agent_type": "coding", "status": "completed",
                                 "runtime_id": 1, "created_at": "", "started_at": "", "completed_at": ""},
                                {}, [], False)
        assert item["engine_uid"] == UID_OPENCODE
        assert item["runtime_id"] == 1

    def test_task_item_missing_column_degrades_empty(self, monkeypatch):
        conn = _db_with_runtimes(uid=False)
        _patch_db(monkeypatch, conn)
        trace = importlib.import_module("src.domains.insight.observability.trace")
        item = trace._task_item({"id": "t1", "agent_type": "coding", "status": "completed",
                                 "runtime_id": 1, "created_at": "", "started_at": "", "completed_at": ""},
                                {}, [], False)
        assert item["engine_uid"] == ""


# ---------- F4 解绑引擎提示 ----------

class TestDetachedEngineHintsUid:
    def test_hints_carry_engine_uid(self, monkeypatch):
        conn = _db_with_runtimes(uid=True)
        conn.execute(_DETACH_LOG)
        conn.execute("INSERT INTO scene_engine_detach_log (scene_row_id, scene_id, node_tag, runtime_id, runtime_name) "
                     "VALUES (11, 'sc-1', 'coding', 1, 'opencode')")
        conn.commit()
        _patch_db(monkeypatch, conn)
        es = importlib.import_module("src.domains.runtime.engine.engine_select")
        hints = es.get_detached_engine_hints(scene_row_id=11)
        assert hints and hints[0].get("runtime_id") == 1
        assert hints[0].get("engine_uid") == UID_OPENCODE

    def test_hints_missing_column_degrades_empty(self, monkeypatch):
        conn = _db_with_runtimes(uid=False)
        conn.execute(_DETACH_LOG)
        conn.execute("INSERT INTO scene_engine_detach_log (scene_row_id, scene_id, node_tag, runtime_id, runtime_name) "
                     "VALUES (11, 'sc-1', 'coding', 1, 'opencode')")
        conn.commit()
        _patch_db(monkeypatch, conn)
        es = importlib.import_module("src.domains.runtime.engine.engine_select")
        hints = es.get_detached_engine_hints(scene_row_id=11)
        assert hints and hints[0].get("engine_uid") == ""
