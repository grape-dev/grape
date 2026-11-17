"""Issue #868 交叉预警守卫测试：engine_uid 派生源与 i5 四段名「改名跟随」机制冲突修正。

背景（Issue #868 评论 id=198269670 / QA 复核 id=198272227）：
- i5 分支（feat/devbox-sandbox-integration5）云引擎 name = 四段式复合名
  `engine_type:tpl_slug:provider:provider_id`，模板/Provider 改名后 name 随 refresh 刷新。
- 原 derive_engine_uid 的 uuid5 自然键含 name 段 → 改名即 uid 漂移，
  击穿 v2 spec §2.3 的「uid 失效面收敛」假设。

修正契约（本文件守卫）：
- ① 云引擎（带不可变身份锚 engine_type/provider_id/template_id）派生时 name 不参与；
  模板/Provider 改名前后 uid 不变；
- ② 普通引擎（无锚）沿用旧公式（name 本身稳定=锚），存量 uid 零漂移（回归守卫）；
- ③ 合流过渡：旧行按旧键（含 name）派生的 uid 与新锚 uid 不一致时，
  rebase_engine_uids_by_anchor / rebase_engine_uid_references 完成重算回填与引用改绑
  （池/能力/任务/会话 uid 轨道不留孤儿）。
"""
import sqlite3
import threading
import uuid

import pytest

from src.kernel.engine_uid import (
    GRAPE_ENGINE_UID_NAMESPACE,
    cloud_anchor_from_runtime_info,
    derive_engine_uid,
    is_uid_str,
)

# i5 四段名样例：engine_type:tpl_slug:provider:provider_id
NAME_TPL_S11 = "hermes:s11:cube:5"
NAME_TPL_S12 = "hermes:s12:cube:5"          # 模板改名（tpl_slug s11→s12）
NAME_PROVIDER_RENAMED = "hermes:s11:cube_new:5"  # Provider 改名（provider 段变，provider_id 不变）

ANCHOR = {"engine_type": "hermes", "provider_id": "5", "template_id": "11"}


def _cloud(name, **over):
    kw = dict(ANCHOR)
    kw.update(over)
    return derive_engine_uid("cu-1", name, "global", "", "", **kw)


# ---------------------------------------------------------------------------
# ① 云引擎：模板/Provider 改名前后 uid 不变
# ---------------------------------------------------------------------------

class TestCloudRenameGuard:
    def test_template_rename_keeps_uid(self):
        a = _cloud(NAME_TPL_S11)
        b = _cloud(NAME_TPL_S12)
        assert a == b, "模板改名导致 engine_uid 漂移（uuid5 键不应含可变 name 段）"

    def test_provider_rename_keeps_uid(self):
        a = _cloud(NAME_TPL_S11)
        b = _cloud(NAME_PROVIDER_RENAMED)
        assert a == b, "Provider 改名导致 engine_uid 漂移"

    def test_display_name_change_keeps_uid(self):
        a = _cloud(NAME_TPL_S11)
        b = _cloud("任意展示名")
        assert a == b, "云引擎展示名变化不应影响 uid"

    def test_anchor_change_changes_uid(self):
        base = _cloud(NAME_TPL_S11)
        assert _cloud(NAME_TPL_S11, template_id="12") != base, "template_id 变化 uid 应变化"
        assert _cloud(NAME_TPL_S11, provider_id="6") != base, "provider_id 变化 uid 应变化"
        assert _cloud(NAME_TPL_S11, engine_type="opencode") != base, "engine_type 变化 uid 应变化"

    def test_uid_form_valid(self):
        assert is_uid_str(_cloud(NAME_TPL_S11))

    def test_same_anchor_two_providers_distinct(self):
        # 同锚不同 credential → 不同 uid（凭据隔离不变）
        kw = dict(ANCHOR)
        a = derive_engine_uid("cu-1", NAME_TPL_S11, "global", "", "", **kw)
        b = derive_engine_uid("cu-2", NAME_TPL_S11, "global", "", "", **kw)
        assert a != b


# ---------------------------------------------------------------------------
# ② 普通引擎：旧公式零漂移（回归守卫）
# ---------------------------------------------------------------------------

class TestLegacyFormulaUnchanged:
    def test_no_anchor_uses_legacy_key(self):
        uid = derive_engine_uid("cu-1", "hermes-worker01", "global", "", "host1")
        key = "|".join(("cu-1", "hermes-worker01", "global", "", "host1"))
        assert uid == str(uuid.uuid5(GRAPE_ENGINE_UID_NAMESPACE, key)), \
            "无锚引擎派生公式被改动（存量 uid 会全量漂移）"

    def test_cloud_anchor_extraction(self):
        rt = {"name": NAME_TPL_S11, "engine_type": "hermes",
              "provider_id": "5", "template_id": "11"}
        assert cloud_anchor_from_runtime_info(rt) == ("hermes", "5", "11", "", "")
        # 键缺失 → 空（普通引擎路径）
        assert cloud_anchor_from_runtime_info({"name": "opencode"}) == ("", "", "", "", "")
        # 别名键：engine / template_db_id（i5 runtime_info 透传口径）
        rt2 = {"name": NAME_TPL_S11, "engine": "hermes",
               "template_db_id": "11", "provider_id": 5}
        assert cloud_anchor_from_runtime_info(rt2) == ("hermes", "5", "11", "", "")
        # 部分锚（仅 engine_type）也视为云身份（宁可换 uid 不留 name 漂移）
        assert cloud_anchor_from_runtime_info({"name": "x", "engine_type": "hermes"}) \
            == ("hermes", "", "", "", "")


# ---------------------------------------------------------------------------
# ③ 合流过渡：旧行旧键 uid → 新锚 uid 的重算回填 + 引用改绑
# ---------------------------------------------------------------------------

_MIN_SCHEMA = """
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    host_name VARCHAR(191) DEFAULT '',
    engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE agent_type_runtime_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type TEXT, runtime_id INTEGER, engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE engine_capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id INTEGER, tag TEXT, engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY, runtime_id INTEGER, engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE devops_agent_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_id INTEGER, engine_uid VARCHAR(36) DEFAULT NULL
);
CREATE TABLE chat_agent_session (
    id TEXT PRIMARY KEY, runtime_id INTEGER, engine_uid VARCHAR(36) DEFAULT NULL
);
"""


class TestAnchorRebase:
    @pytest.fixture()
    def dbconn(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "anchor.db"))
        conn.executescript(_MIN_SCHEMA)
        import src.domains.mgmt.user.db as udb
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        # engine_uid 双读已改走运行域公开读原语（_any_cursor 单库回落 session_manager.db）：
        # 测试单库桩必须同步 patch 主库连接，否则读不到 agent_runtimes
        import src.kernel.session_manager as _ksm
        monkeypatch.setattr(_ksm, "db", conn, raising=True)
        monkeypatch.setattr(_ksm, "db_lock", threading.RLock(), raising=True)
        return udb, conn

    @staticmethod
    def _seed(conn):
        # 旧行：uid 按旧键（含四段名 name）派生
        old_uid = derive_engine_uid("cu-1", NAME_TPL_S11, "global", "", "")
        conn.execute(
            "INSERT INTO runtime_credentials (id, credential_uid) VALUES (9, 'cu-1')")
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name, engine_uid) "
            "VALUES (7, 9, ?, 'global', '', '', ?)", (NAME_TPL_S11, old_uid))
        conn.execute("INSERT INTO agent_type_runtime_pool (agent_type, runtime_id, engine_uid) VALUES ('coding', 7, ?)", (old_uid,))
        conn.execute("INSERT INTO engine_capabilities (runtime_id, tag, engine_uid) VALUES (7, 'all', ?)", (old_uid,))
        conn.execute("INSERT INTO agent_tasks (id, runtime_id, engine_uid) VALUES ('t1', 7, ?)", (old_uid,))
        conn.execute("INSERT INTO task_messages (task_id, engine_uid) VALUES ('t1', ?)", (old_uid,))
        conn.execute("INSERT INTO devops_agent_session (runtime_id, engine_uid) VALUES (7, ?)", (old_uid,))
        conn.execute("INSERT INTO chat_agent_session (id, runtime_id, engine_uid) VALUES ('s1', 7, ?)", (old_uid,))
        conn.commit()
        return old_uid

    def test_rebase_engine_uids_by_anchor(self, dbconn):
        udb, conn = dbconn
        old_uid = self._seed(conn)
        new_uid = _cloud(NAME_TPL_S12)   # 模板改名后的新锚派生
        assert new_uid != old_uid, "前置：旧键 uid 与新锚 uid 应不一致（过渡态）"
        report = udb.rebase_engine_uids_by_anchor(
            conn.cursor(), [{"runtime_id": 7, **ANCHOR}])
        assert report["engines_rebased"] == 1
        for tbl, cond in (("agent_runtimes", "id=7"),
                          ("agent_type_runtime_pool", "runtime_id=7"),
                          ("engine_capabilities", "runtime_id=7"),
                          ("agent_tasks", "id='t1'"),
                          ("task_messages", "task_id='t1'"),
                          ("devops_agent_session", "runtime_id=7"),
                          ("chat_agent_session", "id='s1'")):
            got = conn.execute(f"SELECT engine_uid FROM {tbl} WHERE {cond}").fetchone()[0]
            assert got == new_uid, f"{tbl} uid 轨道未改绑到新锚 uid"
        # 旧 uid 无残留
        for tbl in ("agent_runtimes", "agent_type_runtime_pool", "engine_capabilities",
                    "agent_tasks", "task_messages", "devops_agent_session", "chat_agent_session"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE engine_uid=?", (old_uid,)).fetchone()[0]
            assert n == 0, f"{tbl} 旧 uid 残留"

    def test_rebase_engine_uid_references(self, dbconn):
        udb, conn = dbconn
        old_uid = self._seed(conn)
        new_uid = _cloud(NAME_TPL_S12)
        report = udb.rebase_engine_uid_references(conn.cursor(), old_uid, new_uid)
        assert report["engines_rebased"] == 1
        assert conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=7").fetchone()[0] == new_uid
        assert report["pool_rows"] == 1 and report["capability_rows"] == 1
        assert report["task_rows"] == 1 and report["task_message_rows"] == 1
        assert report["devops_sessions"] == 1 and report["chat_sessions"] == 1

    def test_rebase_same_uid_noop(self, dbconn):
        udb, conn = dbconn
        old_uid = self._seed(conn)
        report = udb.rebase_engine_uid_references(conn.cursor(), old_uid, old_uid)
        assert report["engines_rebased"] == 0

    def test_rename_then_uid_lookup_stable(self, dbconn):
        """改名跟随链路：改名后 uid 不变 → claim-by-uid 寻址不断链。"""
        udb, conn = dbconn
        self._seed(conn)
        # 云 worker 带锚重复注册（i5 合流后形态）：uid-first 语义由派生保证
        uid_before = _cloud(NAME_TPL_S11)
        uid_after = _cloud(NAME_TPL_S12)
        assert uid_before == uid_after
        conn.execute("UPDATE agent_runtimes SET engine_uid=? WHERE id=7", (uid_after,))
        conn.commit()
        assert udb.get_runtime_id_by_engine_uid(uid_after) == 7
