"""Issue #1208 原子契约测试：保存端三层校验 + 双非空拒绝 + 透明 uid 升格（D7/D11/D13）。

覆盖（Issue 正文 §2.6 / §3 验收 8/9 / §4.1 兼容对账）：
- L1 语法硬阻断：未知前缀 400 文案含「未实现的维度」（label: 本身未实现——已特判为「排期中(D20)，本期不可用」，不再误导性提示）；
  值域外（scope:xxx/engine:xxx）400 + 合法值列表；格式错（大写/空格/空值）400；
- 三模式互斥（D7）：ids/uids 双非空 400；selector 与 ids/uids 双非空 400；
- SRE 节点：selector 非空视为已绑定（不触发空绑定 400）；
- 透明 uid 升格（D11）：ids 保存 → 服务端解析写 uids + 清空 ids，回读 uid 形态；
- agent.py _update_scene_engine_ids：绑定值一律写 uids 清 ids。
"""
import json
import sqlite3
import threading

import pytest


# ===== L1 语法校验（engine_select.validate_selector_tags）=====

class TestValidateSelectorTags:
    def test_valid_tags_pass(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        assert validate_selector_tags(["scope:repo", "engine:opencode", "scene:default"]) == ""
        assert validate_selector_tags([]) == ""  # 空 selector 合法（不约束）

    def test_unknown_prefix_rejected_with_label_hint(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        err = validate_selector_tags(["foo:bar"])
        assert "未实现的维度" in err
        # [F10 文案对齐 2026-11-13] label: 前缀本身未实现——已特判为排期提示，
        # 未知前缀不再误导性指引 label:
        assert "自定义标签维度未开放" in err

    def test_scope_value_domain(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        err = validate_selector_tags(["scope:xxx"])
        assert "合法值" in err and "global" in err and "user" in err and "repo" in err

    def test_engine_value_domain(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        err = validate_selector_tags(["engine:notanengine"])
        assert "合法值" in err and "opencode" in err

    def test_scene_dynamic_domain(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        # scene 值域动态：scene_market 活跃行 + default 兜底
        err = validate_selector_tags(["scene:nope"], scene_values=["default", "iac"])
        assert "合法值" in err and "iac" in err
        assert validate_selector_tags(["scene:iac"], scene_values=["default", "iac"]) == ""

    def test_format_errors(self):
        from src.domains.runtime.engine.engine_select import validate_selector_tags
        assert validate_selector_tags(["Scope:Repo"]) != ""       # 大写
        assert validate_selector_tags(["scope:re po"]) != ""      # 空格
        assert validate_selector_tags(["scope:"]) != ""           # 空值
        assert validate_selector_tags(["novalue"]) != ""          # 无冒号


# ===== 保存端三模式互斥 + 升格（scenes.py）=====

@pytest.fixture()
def save_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT '', updated_at TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1);
    CREATE TABLE managed_repos (repo_key TEXT PRIMARY KEY, dev_scene TEXT DEFAULT '');
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    # src/user/db.py 模块级 from-import 绑定（get_engine_uid_by_runtime_id 等读侧）
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "db", c)
    monkeypatch.setattr(udb, "db_lock", threading.RLock())
    from src.api import pipeline_config as pc
    monkeypatch.setattr(pc, "_SCENE_CACHE", {})
    yield c
    c.close()


def _mk_engine(save_db, rid=1, uid="aaaaaaaa-1111-4111-8111-111111111111"):
    save_db.execute("INSERT INTO agent_runtimes (id, name, credential_id, engine_uid) "
                    "VALUES (?,?,1,?)", (rid, "opencode", uid))
    save_db.commit()


class TestThreeModeSave:
    def test_dual_nonempty_ids_uids_rejected(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        nodes = [{"tag": "coding", "engine_runtime_ids": [1],
                  "engine_runtime_uids": ["aaaaaaaa-1111-4111-8111-111111111111"]}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert "400" in str(getattr(ei.value, "status_code", "")) or \
            getattr(ei.value, "status_code", 0) == 400

    def test_selector_with_ids_rejected(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        nodes = [{"tag": "coding", "engine_runtime_ids": [1],
                  "engine_selector": {"tags": ["scope:repo"]}}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400

    def test_selector_with_uids_rejected(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        nodes = [{"tag": "coding", "engine_runtime_uids": ["aaaaaaaa-1111-4111-8111-111111111111"],
                  "engine_selector": {"tags": ["scope:repo"]}}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400

    def test_selector_l1_invalid_rejected_with_values(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        nodes = [{"tag": "coding", "engine_selector": {"tags": ["foo:bar"]}}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400
        # [F10 文案对齐 2026-11-13] 未知前缀走「自定义标签维度未开放」(label: 已特判排期)
        assert "未实现的维度" in str(getattr(ei.value, "detail", ""))

    def test_sre_selector_counts_as_bound(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        # SRE 空 = 无引擎 400；selector 非空 = 已绑定（不触发）
        nodes_bad = [{"tag": "sre", "enabled": True, "engine_runtime_ids": []}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes_bad, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400
        nodes_ok = [{"tag": "sre", "enabled": True,
                     "engine_selector": {"tags": ["scope:user", "scope:repo"]}}]
        _validate_scene_engine_ids(nodes_ok, "gitcode:o/r", "u")  # 不抛

    def test_invalid_uid_rejected(self, save_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        nodes = [{"tag": "coding", "engine_runtime_uids": ["dddddddd-0000-4000-8000-000000000000"]}]
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400


class TestTransparentUidUpgrade:
    def test_ids_upgraded_to_uids_on_save(self, save_db):
        # D11：绑定值保存一律写 uids 并清空 ids（服务端透明升格，前端输出 uid）
        from src.domains.mgmt.routes.scenes import _upgrade_scene_nodes_uid
        _mk_engine(save_db, rid=1, uid="aaaaaaaa-1111-4111-8111-111111111111")
        nodes = [{"tag": "coding", "engine_runtime_ids": [1], "engine_runtime_uids": []}]
        _upgrade_scene_nodes_uid(nodes)
        n = nodes[0]
        assert n["engine_runtime_uids"] == ["aaaaaaaa-1111-4111-8111-111111111111"]
        assert n["engine_runtime_ids"] == []          # 两字段不同时非空

    def test_uid_passthrough_kept(self, save_db):
        from src.domains.mgmt.routes.scenes import _upgrade_scene_nodes_uid
        nodes = [{"tag": "coding", "engine_runtime_ids": [],
                  "engine_runtime_uids": ["aaaaaaaa-1111-4111-8111-111111111111"]}]
        _upgrade_scene_nodes_uid(nodes)
        assert nodes[0]["engine_runtime_uids"] == ["aaaaaaaa-1111-4111-8111-111111111111"]

    def test_dangling_id_not_upgraded(self, save_db):
        # 悬空 id（引擎行不存在）：不猜不静默——保留原 ids（T11 脚本同口径），不强转
        from src.domains.mgmt.routes.scenes import _upgrade_scene_nodes_uid
        nodes = [{"tag": "coding", "engine_runtime_ids": [999], "engine_runtime_uids": []}]
        _upgrade_scene_nodes_uid(nodes)
        assert nodes[0]["engine_runtime_ids"] == [999]
        assert nodes[0]["engine_runtime_uids"] == []

    def test_selector_node_untouched(self, save_db):
        from src.domains.mgmt.routes.scenes import _upgrade_scene_nodes_uid
        nodes = [{"tag": "coding", "engine_runtime_ids": [], "engine_runtime_uids": [],
                  "engine_selector": {"tags": ["scope:repo"]}}]
        _upgrade_scene_nodes_uid(nodes)
        assert nodes[0]["engine_selector"] == {"tags": ["scope:repo"]}

    def test_agents_update_scene_engine_ids_writes_uids(self, save_db):
        # agents.py 会话配置保存链：backends（数字 id）→ 场景 node 写 uids 清 ids
        from src.domains.mgmt.routes.agents import _update_scene_engine_ids
        _mk_engine(save_db, rid=1, uid="aaaaaaaa-1111-4111-8111-111111111111")
        save_db.execute("INSERT INTO devops_pipeline_scene (scene_id, name, nodes_json, is_active, repo_key) "
                        "VALUES ('sc-a','s','[{\"id\":\"coding\",\"tag\":\"coding\",\"engine_runtime_ids\":[]}]',1,'gitcode:o/r')")
        save_db.execute("INSERT INTO managed_repos (repo_key, dev_scene) VALUES ('gitcode:o/r','sc-a')")
        save_db.commit()
        from src.api import pipeline_config as pc
        monkey_tmp = pc._SCENE_CACHE
        pc._SCENE_CACHE = {}
        try:
            _update_scene_engine_ids("gitcode:o/r", "coding", [1])
        finally:
            pc._SCENE_CACHE = monkey_tmp
        row = save_db.execute("SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-a'").fetchone()
        node = json.loads(row[0])[0]
        assert node["engine_runtime_uids"] == ["aaaaaaaa-1111-4111-8111-111111111111"]
        assert node["engine_runtime_ids"] == []
