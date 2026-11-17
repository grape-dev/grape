"""Issue #868 派生源种子分量 id→uid 升格守卫测试（QA 复核 Required，评论 198288768）。

背景（用户评论 id=198288127 / QA 复核 id=198288768）：
- 云引擎派生种子的 provider_id/template_id 两个自增 id 分量在分布式与库恢复场景不可靠
  （多主控聚合撞号 / 部分恢复 id 错配 / 表重建 id 全量换值）。
- 升格方案（与 credential_uid 同构）：i5 侧 grape_t_sandbox_provider_config 加 provider_uid、
  grape_t_sandbox_templates 加 template_uid（UUIDv4，创建时一次性生成、终身不变，backfill 沿
  旧行值按旧 id 映射拷贝、不新发 UUID），worker 注册 payload runtime 条目透传 uid；
  grape 侧种子切换为
  ``credential_uid|engine_type|scope|owner_id|host_name|provider_uid|template_uid``。

本文件守卫（grape 侧契约）：
- ① uid 表示在场（provider_uid + template_uid 成对）→ 种子用 uid 公式，
  **id 分量与 name 均不参与**：表重建/库恢复导致自增 id 换值 → uid 不变；
- ② 过渡兼容：uid 未透传（老 worker）仅有 id 锚 → 沿用 id 公式（T13 行为不变），
  部分 uid（缺一）不切 uid 公式（防两轮漂移）；无锚 → legacy 公式零漂移；
- ③ 升格迁移：旧 uid（id 公式产物）→ 新 uid（uid 公式产物）按「旧 uid→新 uid 映射式」
  回填改绑（rebase_engine_uids_by_anchor 读行内旧 uid，禁止按 runtime_id 重派 JOIN），
  引用轨道整体改绑不留孤儿；
- ④ cloud_anchor_from_runtime_info 提取 uid 表示（含别名）并保留 id 回退。
"""
import sqlite3
import threading
import uuid

import pytest

from src.kernel.engine_uid import (
    GRAPE_ENGINE_UID_NAMESPACE,
    cloud_anchor_from_runtime_info,
    derive_engine_uid,
)

# i5 四段名样例：engine_type:tpl_slug:provider:provider_id
NAME_TPL_S11 = "hermes:s11:cube:5"
ID_ANCHOR = {"engine_type": "hermes", "provider_id": "5", "template_id": "11"}
UID_ANCHOR = {"provider_uid": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
              "template_uid": "1a2b3c4d-5e6f-7a8b-9c0d-e1f2a3b4c5d6"}


def _uid_form(name=NAME_TPL_S11, cred="cu-1", **over):
    kw = dict(ID_ANCHOR)
    kw.update(UID_ANCHOR)
    kw.update(over)
    return derive_engine_uid(cred, name, "global", "", "", **kw)


def _id_form(name=NAME_TPL_S11, cred="cu-1", **over):
    kw = dict(ID_ANCHOR)
    kw.update(over)
    return derive_engine_uid(cred, name, "global", "", "", **kw)


# ---------------------------------------------------------------------------
# ① uid 表示在场：种子切 uid 公式，id 分量不参与（表重建/库恢复守卫）
# ---------------------------------------------------------------------------

class TestUidSeedIgnoresIdComponents:
    def test_table_rebuild_id_renumber_keeps_uid(self):
        """表重建（delete-重插）/ 部分恢复：自增 id 全量换值，uid 不变。"""
        a = _uid_form(provider_id="5", template_id="11")
        b = _uid_form(provider_id="99001", template_id="88002")
        assert a == b, "uid 公式种子不应含自增 id 分量（表重建即漂移）"

    def test_restore_snapshot_id_mismatch_keeps_uid(self):
        """旧快照搬回新库：runtime_info 快照旧 id 错配别行，uid 仍由 uid 锚决定。"""
        a = _uid_form()
        b = _uid_form(provider_id="", template_id="")
        assert a == b

    def test_uid_formula_excludes_name_segment(self):
        a = _uid_form(name=NAME_TPL_S11)
        b = _uid_form(name="hermes:s12:cube_new:99001")
        assert a == b, "uid 公式种子不应含 name 段（改名跟随守卫延续）"

    def test_uid_formula_matches_uuid5_key(self):
        uid = _uid_form()
        key = "|".join(("cu-1", "hermes", "global", "", "",
                        UID_ANCHOR["provider_uid"], UID_ANCHOR["template_uid"]))
        assert uid == str(uuid.uuid5(GRAPE_ENGINE_UID_NAMESPACE, key))

    def test_uid_component_change_changes_uid(self):
        base = _uid_form()
        assert _uid_form(provider_uid=str(uuid.uuid4())) != base
        assert _uid_form(template_uid=str(uuid.uuid4())) != base

    def test_uid_formula_differs_from_id_formula(self):
        """升格切换是一次受控 uid 变更（注册路径改绑/批量回填承接），两公式产物不同。"""
        assert _uid_form() != _id_form()


# ---------------------------------------------------------------------------
# ② 过渡兼容：老 worker 仅透传 id 锚 → id 公式不变；部分 uid 不切换；legacy 零漂移
# ---------------------------------------------------------------------------

class TestTransitionCompat:
    def test_id_only_anchor_keeps_t13_formula(self):
        uid = _id_form()
        key = "|".join(("cu-1", "hermes", "global", "", "", "5", "11"))
        assert uid == str(uuid.uuid5(GRAPE_ENGINE_UID_NAMESPACE, key)), \
            "id 锚（老 worker 未透传 uid）派生公式应保持 T13 行为（过渡零漂移）"

    def test_partial_uid_falls_back_to_id_formula(self):
        """仅 provider_uid 或仅 template_uid（缺一）→ 不切 uid 公式（防两轮漂移）。"""
        a = _id_form()
        b = _id_form(provider_uid=UID_ANCHOR["provider_uid"])     # 缺 template_uid
        c = _id_form(template_uid=UID_ANCHOR["template_uid"])     # 缺 provider_uid
        assert a == b and a == c

    def test_legacy_formula_unchanged(self):
        uid = derive_engine_uid("cu-1", "hermes-worker01", "global", "", "host1")
        key = "|".join(("cu-1", "hermes-worker01", "global", "", "host1"))
        assert uid == str(uuid.uuid5(GRAPE_ENGINE_UID_NAMESPACE, key))


# ---------------------------------------------------------------------------
# ③ 升格迁移：旧 uid（id 公式）→ 新 uid（uid 公式）映射式回填改绑
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


class TestSeedUidUpgradeRebase:
    @pytest.fixture()
    def dbconn(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "seed-uid-upgrade.db"))
        conn.executescript(_MIN_SCHEMA)
        import src.domains.mgmt.user.db as udb
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        return udb, conn

    @staticmethod
    def _seed(conn):
        """旧行：uid 按 id 公式（T13 过渡态）派生落库。"""
        old_uid = _id_form()
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

    def test_rebase_by_uid_anchor_mapping_backfill(self, dbconn):
        """映射式回填：行内旧 uid → uid 公式新 uid，全引用轨道改绑，旧 uid 无残留。"""
        udb, conn = dbconn
        old_uid = self._seed(conn)
        new_uid = _uid_form()
        assert new_uid != old_uid, "前置：id 公式与 uid 公式产物应不一致（过渡态）"
        report = udb.rebase_engine_uids_by_anchor(
            conn.cursor(), [{"runtime_id": 7, **ID_ANCHOR, **UID_ANCHOR}])
        assert report["engines_rebased"] == 1
        for tbl, cond in (("agent_runtimes", "id=7"),
                          ("agent_type_runtime_pool", "runtime_id=7"),
                          ("engine_capabilities", "runtime_id=7"),
                          ("agent_tasks", "id='t1'"),
                          ("task_messages", "task_id='t1'"),
                          ("devops_agent_session", "runtime_id=7"),
                          ("chat_agent_session", "id='s1'")):
            got = conn.execute(f"SELECT engine_uid FROM {tbl} WHERE {cond}").fetchone()[0]
            assert got == new_uid, f"{tbl} uid 轨道未改绑到 uid 公式新 uid"
        for tbl in ("agent_runtimes", "agent_type_runtime_pool", "engine_capabilities",
                    "agent_tasks", "task_messages", "devops_agent_session", "chat_agent_session"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE engine_uid=?", (old_uid,)).fetchone()[0]
            assert n == 0, f"{tbl} 旧 uid 残留"

    def test_rebase_id_anchor_rows_still_id_formula(self, dbconn):
        """老清单（仅 id 锚，无 uid）：回填仍按 id 公式（幂等，不误切 uid 公式）。"""
        udb, conn = dbconn
        old_uid = self._seed(conn)
        target = _id_form(name=NAME_TPL_S11)
        assert target == old_uid  # id 锚重派与行内旧 uid 一致 → no-op 改绑
        report = udb.rebase_engine_uids_by_anchor(
            conn.cursor(), [{"runtime_id": 7, **ID_ANCHOR}])
        assert report["engines_rebased"] == 0
        assert conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=7").fetchone()[0] == old_uid

    def test_rebase_then_claim_by_new_uid(self, dbconn):
        """库恢复场景：回填后按新 uid 寻址命中（claim-by-uid v2 前置不断链）。"""
        udb, conn = dbconn
        self._seed(conn)
        new_uid = _uid_form()
        udb.rebase_engine_uids_by_anchor(
            conn.cursor(), [{"runtime_id": 7, **ID_ANCHOR, **UID_ANCHOR}])
        assert udb.get_runtime_id_by_engine_uid(new_uid) == 7


# ---------------------------------------------------------------------------
# ④ cloud_anchor_from_runtime_info：uid 表示提取（含别名）+ id 回退
# ---------------------------------------------------------------------------

class TestAnchorExtractionUidRepresentation:
    def test_uid_keys_extracted(self):
        rt = {"name": NAME_TPL_S11, "engine_type": "hermes",
              "provider_id": "5", "template_id": "11",
              "provider_uid": UID_ANCHOR["provider_uid"],
              "template_uid": UID_ANCHOR["template_uid"]}
        et, pid, tid, puid, tuid = cloud_anchor_from_runtime_info(rt)
        assert (et, pid, tid) == ("hermes", "5", "11")
        assert puid == UID_ANCHOR["provider_uid"]
        assert tuid == UID_ANCHOR["template_uid"]

    def test_uid_only_without_ids(self):
        """i5 升格后可不透传 id（uid 已足够定位行）。"""
        rt = {"name": NAME_TPL_S11, "engine": "hermes",
              "provider_uid": UID_ANCHOR["provider_uid"],
              "template_uid": UID_ANCHOR["template_uid"]}
        et, pid, tid, puid, tuid = cloud_anchor_from_runtime_info(rt)
        assert (et, pid, tid) == ("hermes", "", "")
        assert puid == UID_ANCHOR["provider_uid"] and tuid == UID_ANCHOR["template_uid"]

    def test_id_fallback_when_uid_absent(self):
        rt = {"name": NAME_TPL_S11, "engine_type": "hermes",
              "provider_id": "5", "template_id": "11"}
        assert cloud_anchor_from_runtime_info(rt) == ("hermes", "5", "11", "", "")

    def test_no_anchor_all_empty(self):
        assert cloud_anchor_from_runtime_info({"name": "opencode"}) == ("", "", "", "", "")

    def test_non_dict_input(self):
        assert cloud_anchor_from_runtime_info(None) == ("", "", "", "", "")

    def test_int_values_normalized(self):
        rt = {"name": NAME_TPL_S11, "engine_type": "hermes", "provider_id": 5,
              "template_id": 11,
              "provider_uid": UID_ANCHOR["provider_uid"].upper(),
              "template_uid": UID_ANCHOR["template_uid"]}
        et, pid, tid, puid, tuid = cloud_anchor_from_runtime_info(rt)
        assert (et, pid, tid) == ("hermes", "5", "11")
        assert puid == UID_ANCHOR["provider_uid"], "uid 提取应归一小写规范形"
