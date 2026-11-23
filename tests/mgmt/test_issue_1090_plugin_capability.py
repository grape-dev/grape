"""Issue #1090 — [插件市场-C2] Prompt 能力清单 + D1-a 协商

测试覆盖（对照 Spec Acceptance Criteria）：
- 运行时插件状态读取（B5/C1 契约对接 + 降级先行）：installed_plugins 表缺失/读取失败 → 空清单不抛异常
- 清单注入：任务下发 Prompt 含插件能力清单段（空清单有占位说明）
- 降级路径：状态读取失败时 Prompt 仍构建、降级提示可见，核心链路不阻塞
- D1-a 协商：缺插件报告 + 建议安装项结构化输出（B3 统一确认单契约），B3 未合入时降级 comment 通道
- 补装恢复：状态更新/缓存失效后，下一次任务下发 Prompt 含新插件能力
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

TEST_REPO = "gitcode:test-org1090/test-repo"


@pytest.fixture(autouse=True)
def _bind_plugins_port():
    """prompt_builder 经 kernel.ports.plugins 取插件能力（R1 端口化）——测试需绑定端口。

    Aggregate 转发到 src.domains.runtime.engine.plugin_state 的模块属性，
    monkeypatch.setattr(plugin_state, ...) 与生产同面可见。
    """
    from src.kernel.ports import plugins as plugins_port
    from src.domains.runtime.engine import plugin_state
    plugins_port.bind(plugin_state, replace=True)
    yield


@pytest.fixture(autouse=True)
def _clean_plugin_state():
    """每个用例前后清理插件契约表 + 清空模块级缓存（防用例间污染）。"""
    from src.domains.runtime.engine import plugin_state
    plugin_state.invalidate_plugin_cache()
    yield
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM installed_plugins WHERE repo_key=?", (TEST_REPO,))
            db.commit()
    except Exception:
        pass  # 降级用例会 DROP 契约表，容忍表缺失
    plugin_state.invalidate_plugin_cache()


def _create_installed_plugins_table():
    """B5 契约表（#1088：registry 注册/心跳上报 installed_plugins）。

    测试自建表模拟 B5 合入后的物化状态；真实表由 B5 链路创建。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS installed_plugins ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " repo_key TEXT DEFAULT '',"
            " runtime_id INTEGER DEFAULT 0,"
            " name TEXT NOT NULL,"
            " version TEXT DEFAULT '',"
            " fingerprint TEXT DEFAULT '',"
            " capabilities TEXT DEFAULT '',"
            " hook_types TEXT DEFAULT '',"
            " health TEXT DEFAULT 'unknown',"
            " updated_at TEXT DEFAULT (datetime('now')))"
        )
        db.commit()


def _insert_plugin(name, version="1.0.0", capabilities="CLI 调用示例产品",
                   hook_types="scene_node", health="healthy"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO installed_plugins (repo_key, runtime_id, name, version, fingerprint,"
            " capabilities, hook_types, health) VALUES (?,?,?,?,?,?,?,?)",
            (TEST_REPO, 0, name, version, "fp-" + name, capabilities, hook_types, health)
        )
        db.commit()


class TestPluginStateContract:
    """切片1：运行时插件状态读取（B5/C1 契约对接 + 降级先行）。"""

    def test_installed_missing_table_degrades_empty(self):
        """B5 未合入（表不存在）→ 空清单、不抛异常（契约对接 + 降级先行）。"""
        from src.domains.runtime.engine import plugin_state
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS installed_plugins")
            db.commit()
        plugin_state.invalidate_plugin_cache()
        assert plugin_state.get_installed_plugins(TEST_REPO) == []

    def test_installed_reads_contract_rows(self):
        """B5 合入后：读取物化的 installed_plugins 行 → 归一化插件条目。"""
        from src.domains.runtime.engine import plugin_state
        _create_installed_plugins_table()
        _insert_plugin("@huawei/devkit", version="2.1.0",
                       capabilities="devkit CLI", hook_types="scene_node,skill_ref")
        items = plugin_state.get_installed_plugins(TEST_REPO)
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "@huawei/devkit"
        assert item["version"] == "2.1.0"
        assert item["status"] == "installed"
        assert "scene_node" in item["hooks"]

    def test_declared_reads_scene_node_plugins(self, monkeypatch):
        """C1 契约：scene.json node.plugins 声明 → declared 条目。"""
        from src.domains.runtime.engine import plugin_state
        monkeypatch.setattr(
            "src.api.pipeline_config.get_pipeline_config",
            lambda scene_id, repo_key="": {
                "nodes": [{"skill": "coding", "plugins": ["@huawei/obs"]}]
            },
            raising=False,
        )
        items = plugin_state.get_declared_plugins("scene-x", repo_key=TEST_REPO)
        assert len(items) == 1
        assert items[0]["name"] == "@huawei/obs"
        assert items[0]["status"] == "declared"
        assert "scene_node" in items[0]["hooks"]

    def test_declared_without_plugins_field_degrades_empty(self, monkeypatch):
        """C1 未合入/节点未声明 plugins → 空清单、不抛异常。"""
        from src.domains.runtime.engine import plugin_state
        monkeypatch.setattr(
            "src.api.pipeline_config.get_pipeline_config",
            lambda scene_id, repo_key="": {"nodes": [{"skill": "coding"}]},
            raising=False,
        )
        assert plugin_state.get_declared_plugins("scene-x", repo_key=TEST_REPO) == []

    def test_collect_merges_installed_and_declared(self, monkeypatch):
        """汇总：installed + declared 合并去重（同名以 installed 优先）。"""
        from src.domains.runtime.engine import plugin_state
        _create_installed_plugins_table()
        _insert_plugin("@huawei/devkit")
        monkeypatch.setattr(
            "src.api.pipeline_config.get_pipeline_config",
            lambda scene_id, repo_key="": {
                "nodes": [{"skill": "coding", "plugins": ["@huawei/devkit", "@huawei/obs"]}]
            },
            raising=False,
        )
        items = plugin_state.collect_plugin_capabilities(TEST_REPO, scene_id="scene-x")
        names = [i["name"] for i in items]
        assert names.count("@huawei/devkit") == 1
        assert "@huawei/obs" in names
        by_name = {i["name"]: i for i in items}
        assert by_name["@huawei/devkit"]["status"] == "installed"
        assert by_name["@huawei/obs"]["status"] == "declared"

    def test_read_failure_never_raises(self, monkeypatch):
        """状态读取链路任何异常 → 空清单（降级不阻塞任务下发）。"""
        from src.domains.runtime.engine import plugin_state

        def _boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(plugin_state, "get_installed_plugins", _boom)
        monkeypatch.setattr(plugin_state, "get_declared_plugins", _boom)
        assert plugin_state.collect_plugin_capabilities(TEST_REPO) == []


EVT = {
    "type": "issue_opened", "source": "gitcode", "repository": "test-org1090/test-repo",
    "repo_key": TEST_REPO,
    "title": "t", "body": "b", "actor": "a", "issue_id": "#1090", "payload": {},
}


def _patch_collect(monkeypatch, items):
    from src.domains.runtime.engine import plugin_state
    monkeypatch.setattr(plugin_state, "collect_plugin_capabilities",
                        lambda *a, **k: list(items))
    monkeypatch.setattr("src.prompt_builder._build_scene_advanced_options",
                        lambda *a, **k: "")
    monkeypatch.setattr("src.api.session_store.get_issue_scene",
                        lambda *a, **k: ("", None), raising=False)


class TestPromptCapabilitySection:
    """切片2：任务下发 Prompt 注入插件能力清单段（Spec AC ①②）。"""

    def test_prompt_contains_section_with_entries(self, monkeypatch):
        """有插件 → Prompt 含能力清单独立段 + 插件名/能力描述/挂点类型。"""
        _patch_collect(monkeypatch, [
            {"name": "@huawei/devkit", "version": "2.1.0", "capabilities": "devkit CLI",
             "hooks": ["scene_node", "skill_ref"], "status": "installed",
             "health": "healthy", "fingerprint": "fp-x", "source": "installed_plugins"},
        ])
        from src import prompt_builder
        prompt = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                          agent_type="coding")
        assert "## Plugin Capabilities" in prompt
        assert "@huawei/devkit" in prompt
        assert "devkit CLI" in prompt
        assert "scene_node" in prompt

    def test_prompt_empty_list_has_placeholder(self, monkeypatch):
        """空清单 → 占位说明（避免静默缺失）。"""
        _patch_collect(monkeypatch, [])
        from src import prompt_builder
        prompt = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                          agent_type="coding")
        assert "## Plugin Capabilities" in prompt
        assert "无可用插件" in prompt

    def test_prompt_read_failure_degrades_visible(self, monkeypatch):
        """读取失败 → Prompt 仍构建（核心链路不阻塞）+ 降级提示可见。"""
        from src.domains.runtime.engine import plugin_state
        from src import prompt_builder

        def _boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(plugin_state, "collect_plugin_capabilities", _boom)
        monkeypatch.setattr("src.prompt_builder._build_scene_advanced_options",
                            lambda *a, **k: "")
        monkeypatch.setattr("src.api.session_store.get_issue_scene",
                            lambda *a, **k: ("", None), raising=False)
        prompt = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                          agent_type="coding")
        assert "## Plugin Capabilities" in prompt
        assert "无可用插件" in prompt
        assert "degraded" in prompt.lower()

    def test_section_skipped_for_non_executing_agents(self, monkeypatch):
        """会话类 agent（vod/issue）不下发执行任务 → 不注入清单段（防 Prompt 膨胀）。"""
        _patch_collect(monkeypatch, [
            {"name": "@huawei/devkit", "version": "1.0.0", "capabilities": "c",
             "hooks": ["scene_node"], "status": "installed", "health": "healthy",
             "fingerprint": "f", "source": "installed_plugins"},
        ])
        from src import prompt_builder
        prompt = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["issue"],
                                                          agent_type="issue")
        assert "## Plugin Capabilities" not in prompt


class TestPluginNegotiation:
    """切片3：D1-a 缺插件协商（Spec AC ③：报告 + 建议安装项走统一确认单）。"""

    def test_missing_report_identifies_missing(self):
        """任务所需插件 vs 已装清单 → 结构化缺插件报告。"""
        from src.domains.runtime.engine.plugin_negotiation import build_missing_plugin_report
        installed = [{"name": "@huawei/devkit", "status": "installed"}]
        report = build_missing_plugin_report(
            ["@huawei/devkit", "@huawei/obs"],
            installed=installed, task_ref="#1090")
        assert report["type"] == "missing_plugin_report"
        assert report["task_ref"] == "#1090"
        assert report["degraded"] is True
        names = [m["name"] for m in report["missing"]]
        assert "@huawei/obs" in names
        assert "@huawei/devkit" not in names
        assert set(report["installed_names"]) == {"@huawei/devkit"}

    def test_install_request_matches_b3_contract(self):
        """建议安装项对齐 B3 统一确认单契约字段（name/namespace/version/credentials/reason/task）。"""
        from src.domains.runtime.engine.plugin_negotiation import build_missing_plugin_report, build_install_request
        report = build_missing_plugin_report(
            [{"name": "@huawei/obs", "reason": "task needs OBS upload"}],
            installed=[], task_ref="#1090")
        request = build_install_request(report, task_ref="#1090")
        assert request["type"] == "plugin_install_request"
        item = request["items"][0]
        assert item["name"] == "@huawei/obs"
        assert item["namespace"] == "@huawei"
        assert "reason" in item and "version" in item and "credentials_required" in item
        assert request["source_task"] == "#1090"
        assert request["channel"] == "confirmation_card"

    def test_submit_degrades_without_market_api(self):
        """B3/市场服务端未合入（契约模块缺失）→ 降级 comment 通道、不抛异常。"""
        from src.domains.runtime.engine import plugin_negotiation
        request = {"type": "plugin_install_request", "items": [{"name": "@huawei/obs"}],
                   "source_task": "#1090", "channel": "confirmation_card"}
        result = plugin_negotiation.submit_install_request(request)
        assert result["submitted"] is False
        assert result["channel"] == "comment"
        assert result["request"] == request

    def test_submit_uses_market_api_when_available(self, monkeypatch):
        """契约对接：市场服务端 API 可用 → 走统一确认单提交。"""
        import types
        from src.domains.runtime.engine import plugin_negotiation
        fake = types.ModuleType("src.api.plugin_market")
        fake.create_plugin_install_request = lambda req: {"id": "req-1", "status": "pending_confirm"}
        monkeypatch.setitem(sys.modules, "src.api.plugin_market", fake)
        request = {"type": "plugin_install_request", "items": [{"name": "@huawei/obs"}],
                   "source_task": "#1090", "channel": "confirmation_card"}
        result = plugin_negotiation.submit_install_request(request)
        assert result["submitted"] is True
        assert result["channel"] == "confirmation_card"
        assert result["confirm_ref"]["id"] == "req-1"

    def test_negotiation_comment_visible(self):
        """降级 comment 通道：协商文本含缺插件报告 + 建议安装项（用户可见）。"""
        from src.domains.runtime.engine.plugin_negotiation import (
            build_missing_plugin_report, build_install_request, render_negotiation_comment)
        report = build_missing_plugin_report(["@huawei/obs"], installed=[], task_ref="#1090")
        request = build_install_request(report, task_ref="#1090")
        text = render_negotiation_comment(report, request)
        assert "@huawei/obs" in text
        assert "missing_plugin_report" in text
        assert "@[确认]" in text or "确认单" in text
        assert "[degraded]" in text


class TestPluginRecoveryLoop:
    """切片4：补装恢复闭环（Spec AC ④：经确认单补装后能力恢复）。"""

    def test_recovery_after_install_next_dispatch_contains_plugin(self, monkeypatch):
        """补装前占位 → 确认单补装（B5 状态回写 + 恢复触发）→ 下次任务下发含新能力。"""
        from src.domains.runtime.engine import plugin_state
        from src import prompt_builder
        monkeypatch.setattr("src.prompt_builder._build_scene_advanced_options",
                            lambda *a, **k: "")
        monkeypatch.setattr("src.api.session_store.get_issue_scene",
                            lambda *a, **k: ("", None), raising=False)
        _create_installed_plugins_table()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM installed_plugins")
            db.commit()
        plugin_state.invalidate_plugin_cache()
        # 补装前：任务下发 Prompt 为空清单占位
        p1 = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                      agent_type="coding")
        assert "无可用插件" in p1
        # 确认单补装完成：B5 状态回写 + 恢复闭环触发
        _insert_plugin("@huawei/devkit", version="2.1.0")
        plugin_state.notify_plugin_installed(TEST_REPO, "@huawei/devkit")
        # 后续任务下发：能力恢复（占位消失、新插件可见）
        p2 = prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                      agent_type="coding")
        assert "@huawei/devkit" in p2
        assert "无可用插件" not in p2


# ===== 切片5：数据源链补齐 #1089 真实契约（agent_runtimes 自报 / plugin 注册表回退）=====

def _ensure_runtime_contract_schema():
    """C1（#1089）契约形态：agent_runtimes.installed_plugins 自报列 + plugin 注册表。

    测试自建模拟合入后状态；真实列/表由 #1089 挂点④⑤（B5 物化链路）创建。
    """
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()}
        if "installed_plugins" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN installed_plugins TEXT DEFAULT '[]'")
        c.execute(
            "CREATE TABLE IF NOT EXISTS grape_t_plugin ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL,"
            " display_name TEXT DEFAULT '',"
            " version TEXT DEFAULT '1.0.0',"
            " description TEXT DEFAULT '',"
            " manifest TEXT DEFAULT '{}',"
            " bundled_skills TEXT DEFAULT '[]',"
            " credentials TEXT DEFAULT '[]',"
            " status TEXT DEFAULT 'uninstalled',"
            " scope TEXT DEFAULT 'official',"
            " owner_id TEXT DEFAULT '',"
            " installed_at TEXT DEFAULT '',"
            " created_at TEXT DEFAULT (datetime('now')),"
            " updated_at TEXT DEFAULT (datetime('now')),"
            " UNIQUE (name, scope, owner_id))"
        )
        db.commit()


def _mk_registry_plugin(name, status="installed", display="", desc=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO grape_t_plugin (name, display_name, version, description, status)"
            " VALUES (?,?,?,?,?)",
            (name, display or name, "1.0.0", desc or f"{name} 能力说明", status))
        db.commit()


def _mk_runtime_row(installed: list) -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, installed_plugins, status, healthy)"
            " VALUES (0, 'plug-1090-engine', ?, 'online', 1)",
            (json.dumps(installed),))
        rid = c.lastrowid
        db.commit()
    return rid


def _rm_runtime_row(rid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        db.commit()


def _cleanup_contract_rows():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name='plug-1090-engine'")
        c.execute("DELETE FROM grape_t_plugin WHERE name LIKE 'plug-1090-%'")
        db.commit()


class TestRealContractSourceChain:
    """切片5：B5 物化表缺失时按 #1089 真实契约回退（自报→注册表），全部来源缺失降级空清单。"""

    def setup_method(self):
        _ensure_runtime_contract_schema()
        _cleanup_contract_rows()

    def teardown_method(self):
        _cleanup_contract_rows()

    def test_bound_engine_reads_self_report(self):
        """绑定引擎（runtime_id>0）：agent_runtimes 自报为事实源（B5/C1 挂点④）。"""
        from src.domains.runtime.engine import plugin_state
        _create_installed_plugins_table()  # B5 物化表存在但无该 repo 行 → 回退自报
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM installed_plugins WHERE repo_key=?", (TEST_REPO,))
            db.commit()
        rid = _mk_runtime_row([
            {"name": "plug-1090-self", "version": "1.2.0", "status": "installed"},
            "garbage", {"version": "0.1"}, {"name": "  "},
        ])
        try:
            items = plugin_state.get_installed_plugins(TEST_REPO, runtime_id=rid)
            assert [i["name"] for i in items] == ["plug-1090-self"]
            assert items[0]["version"] == "1.2.0"
            assert items[0]["source"] == "runtime_self_report"
            assert "runtime_installed" in items[0]["hooks"]
        finally:
            _rm_runtime_row(rid)

    def test_bound_engine_empty_self_report_is_authoritative(self):
        """绑定引擎自报空清单 → 即该引擎无插件（空清单也权威，不回退注册表）。"""
        from src.domains.runtime.engine import plugin_state
        _mk_registry_plugin("plug-1090-registry-only")
        rid = _mk_runtime_row([])
        try:
            items = plugin_state.get_installed_plugins(TEST_REPO, runtime_id=rid)
            assert items == []
        finally:
            _rm_runtime_row(rid)

    def test_bound_engine_malformed_self_report_falls_to_registry(self):
        """自报 JSON 损坏（契约违规）→ 不抛异常，回退注册表口径。"""
        from src.domains.runtime.engine import plugin_state
        _mk_registry_plugin("plug-1090-fallback")
        rid = _mk_runtime_row([])
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET installed_plugins='not-json' WHERE id=?", (rid,))
            db.commit()
        try:
            items = plugin_state.get_installed_plugins(TEST_REPO, runtime_id=rid)
            assert "plug-1090-fallback" in [i["name"] for i in items]
        finally:
            _rm_runtime_row(rid)

    def test_broadcast_falls_back_to_plugin_registry(self):
        """广播/未绑定（runtime_id=0）：B5 物化表缺失 → plugin 注册表 installed 条目。"""
        from src.domains.runtime.engine import plugin_state
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS installed_plugins")
            db.commit()
        _mk_registry_plugin("plug-1090-broadcast", display="广播插件", desc="注册表能力")
        items = plugin_state.get_installed_plugins(TEST_REPO)
        names = [i["name"] for i in items]
        assert "plug-1090-broadcast" in names
        item = next(i for i in items if i["name"] == "plug-1090-broadcast")
        assert item["status"] == "installed"
        assert item["capabilities"] == "注册表能力"
        assert item["source"] == "grape_t_plugin_registry"

    def test_registry_ignores_uninstalled(self):
        """注册表回退只取 status='installed'，未安装条目不进清单。"""
        from src.domains.runtime.engine import plugin_state
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS installed_plugins")
            db.commit()
        _mk_registry_plugin("plug-1090-off", status="uninstalled")
        items = plugin_state.get_installed_plugins(TEST_REPO)
        assert "plug-1090-off" not in [i["name"] for i in items]

    def test_bound_engine_without_self_report_row_falls_to_registry(self):
        """绑定引擎但行不存在（runtime 已注销）→ 回退注册表，不抛异常。"""
        from src.domains.runtime.engine import plugin_state
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS installed_plugins")
            db.commit()
        _mk_registry_plugin("plug-1090-lost-engine")
        items = plugin_state.get_installed_plugins(TEST_REPO, runtime_id=9991090)
        assert "plug-1090-lost-engine" in [i["name"] for i in items]

    def test_all_sources_missing_degrades_empty(self):
        """B5 表 + 自报 + 注册表全部不可用 → 空清单降级，不抛异常不阻塞。"""
        from src.domains.runtime.engine import plugin_state
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS installed_plugins")
            db.commit()
        rid = _mk_runtime_row([])
        try:
            items = plugin_state.get_installed_plugins(TEST_REPO, runtime_id=rid)
            assert items == []
        finally:
            _rm_runtime_row(rid)


# ===== 切片6：D1-a 对接 #1089 真实统一确认队列 + Agent 可调用端点 =====

_REQUESTS_DDL_1090 = (
    "CREATE TABLE IF NOT EXISTS plugin_install_requests ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " plugin_name TEXT NOT NULL,"
    " version TEXT DEFAULT '',"
    " source TEXT DEFAULT 'skill_ref',"
    " runtime_id INTEGER DEFAULT 0,"
    " repo_key TEXT DEFAULT '',"
    " agent_type TEXT DEFAULT '',"
    " status TEXT DEFAULT 'queued',"
    " created_at TEXT DEFAULT (datetime('now')),"
    " updated_at TEXT DEFAULT (datetime('now')))"
)


def _ensure_request_queue_table():
    with db_lock:
        c = db.cursor()
        c.execute(_REQUESTS_DDL_1090)
        db.commit()


def _clean_request_queue():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM plugin_install_requests WHERE plugin_name LIKE 'plug-1090-%'")
        db.commit()


def _request_payload(names):
    return {"type": "plugin_install_request",
            "items": [{"name": n, "reason": "required by task"} for n in names],
            "source_task": "#1090", "channel": "confirmation_card"}


class TestRealQueueNegotiation:
    """切片6：submit_install_request 源链补 #1089 统一确认队列（B3 确认单落地队列）。"""

    def setup_method(self):
        _ensure_request_queue_table()
        _clean_request_queue()

    def teardown_method(self):
        _clean_request_queue()

    def test_submit_enqueues_unified_queue(self):
        """市场契约未合入 → 入 plugin_install_requests 统一队列（source=agent_report）。"""
        from src.domains.runtime.engine import plugin_negotiation
        result = plugin_negotiation.submit_install_request(
            _request_payload(["plug-1090-obs"]),
            repo_key=TEST_REPO, agent_type="coding")
        assert result["submitted"] is True
        assert result["channel"] == "unified_queue"
        assert result["queued"] == ["plug-1090-obs"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT source, repo_key, agent_type, status FROM plugin_install_requests "
                "WHERE plugin_name='plug-1090-obs'").fetchone()
        assert row == ("agent_report", TEST_REPO, "coding", "queued")

    def test_submit_queue_idempotent(self):
        """同插件已有 queued 请求 → 幂等去重不重复入队，回执标注 deduped。"""
        from src.domains.runtime.engine import plugin_negotiation
        first = plugin_negotiation.submit_install_request(
            _request_payload(["plug-1090-dup"]), repo_key=TEST_REPO)
        second = plugin_negotiation.submit_install_request(
            _request_payload(["plug-1090-dup"]), repo_key=TEST_REPO)
        assert first["queued"] == ["plug-1090-dup"]
        assert second["queued"] == []
        assert second["deduped"] == ["plug-1090-dup"]
        with db_lock:
            n = db.cursor().execute(
                "SELECT COUNT(*) FROM plugin_install_requests "
                "WHERE plugin_name='plug-1090-dup'").fetchone()[0]
        assert n == 1

    def test_submit_queue_missing_falls_to_comment(self):
        """统一队列表也未就绪（B3/B5 均未合入）→ 降级 comment 通道，不抛异常。"""
        from src.domains.runtime.engine import plugin_negotiation
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS plugin_install_requests")
            db.commit()
        result = plugin_negotiation.submit_install_request(
            _request_payload(["plug-1090-none"]), repo_key=TEST_REPO)
        assert result["submitted"] is False
        assert result["channel"] == "comment"
        _ensure_request_queue_table()  # 恢复后续用例契约形态


UID1090 = 91090


class TestAgentNegotiationEndpoints:
    """切片6：Agent 可调用协商端点（session token 面）——报告缺插件/查询当前清单。"""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user_or_api_key
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {
            "id": UID1090, "username": "agent1090", "auth_type": "session_token",
            "repo_key": TEST_REPO, "agent_type": "coding", "session_key": "sk-1090"}
        yield TestClient(app)
        app.dependency_overrides.clear()

    def setup_method(self):
        _ensure_runtime_contract_schema()
        _ensure_request_queue_table()
        _clean_request_queue()
        _cleanup_contract_rows()

    def teardown_method(self):
        _clean_request_queue()
        _cleanup_contract_rows()

    def test_report_missing_endpoint_enqueues(self, client):
        """POST /api/plugins/agent/report-missing：报告+建议安装项入统一确认队列。"""
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-1090-obs", "reason": "task needs OBS upload"}],
            "task_ref": "#1090"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["report"]["type"] == "missing_plugin_report"
        assert data["request"]["items"][0]["name"] == "plug-1090-obs"
        assert data["submit"]["channel"] == "unified_queue"
        # 会话 token 作用域：repo_key/agent_type 取自 token，不接受越库上报
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_key, agent_type, source FROM plugin_install_requests "
                "WHERE plugin_name='plug-1090-obs'").fetchone()
        assert row == (TEST_REPO, "coding", "agent_report")

    def test_report_missing_endpoint_degrades_non_blocking(self, client):
        """统一队列未就绪 → 仍 200 + comment 降级回执（核心链路不阻塞）。"""
        with db_lock:
            c = db.cursor()
            c.execute("DROP TABLE IF EXISTS plugin_install_requests")
            db.commit()
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-1090-degraded", "reason": "r"}],
            "task_ref": "#1090"})
        assert resp.status_code == 200
        assert resp.json()["submit"]["channel"] == "comment"
        _ensure_request_queue_table()

    def test_installed_endpoint_returns_current_list(self, client):
        """GET /api/plugins/agent/installed：返回当前清单（同任务恢复刷新，绕过缓存）。"""
        from src.domains.runtime.engine import plugin_state
        _mk_registry_plugin("plug-1090-refreshed", desc="刷新后能力")
        # 预埋陈旧缓存：GET 必须反映最新物化状态（恢复闭环同任务刷新）
        plugin_state._CAP_CACHE[(TEST_REPO, "")] = (time.time(), [])
        resp = client.get("/api/plugins/agent/installed")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["plugins"]]
        assert "plug-1090-refreshed" in names


ACL_REPO_1090 = "gitcode:agent1090/acl-repo"
ACL_UID_OWNER = 91092
ACL_UID_STRANGER = 91093


class TestAgentEndpointRepoAccess:
    """切片2（QA 复审硬违规②）：非 session_token（JWT）路径补 check_repo_access，
    与相邻双作用域端点（agents/events/dashboard）鉴权惯例一致；session_token
    仍由 token 作用域限定，不走归属校验。"""

    @pytest.fixture()
    def acl_client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user_or_api_key
        from src.domains.mgmt.user import db as user_db
        user_db.add_managed_repo(ACL_REPO_1090, "agent1090", "acl-repo")
        user_db.mark_user_repo_join(ACL_UID_OWNER, ACL_REPO_1090, is_join=True, is_admin=True)
        current_uid = {"id": ACL_UID_OWNER}

        def _fake_jwt_user():
            return {"id": current_uid["id"], "username": "agent1090", "auth_type": "jwt"}

        app.dependency_overrides[get_current_user_or_api_key] = _fake_jwt_user
        yield TestClient(app), current_uid
        app.dependency_overrides.clear()

    def setup_method(self):
        _ensure_runtime_contract_schema()
        _ensure_request_queue_table()
        _clean_request_queue()
        _cleanup_contract_rows()

    def teardown_method(self):
        _clean_request_queue()
        _cleanup_contract_rows()

    def test_report_missing_jwt_stranger_403(self, acl_client):
        """JWT 用户对未归属仓库上报缺插件 → 403，且不入补装队列。"""
        client, uid = acl_client
        uid["id"] = ACL_UID_STRANGER
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-1090-acl"}], "repo_key": ACL_REPO_1090})
        assert resp.status_code == 403, resp.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM plugin_install_requests "
                "WHERE plugin_name='plug-1090-acl'").fetchone()
        assert row[0] == 0

    def test_installed_jwt_stranger_403(self, acl_client):
        """JWT 用户对未归属仓库读插件清单 → 403。"""
        client, uid = acl_client
        uid["id"] = ACL_UID_STRANGER
        resp = client.get("/api/plugins/agent/installed", params={"repo_key": ACL_REPO_1090})
        assert resp.status_code == 403, resp.text

    def test_report_missing_jwt_owner_passes_acl(self, acl_client):
        """JWT 仓库归属人上报缺插件 → 越过归属校验，正常入队（200）。"""
        client, _ = acl_client
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-1090-acl-owner"}], "repo_key": ACL_REPO_1090})
        assert resp.status_code == 200, resp.text
        assert resp.json()["submit"]["channel"] == "unified_queue"


class TestInstalledScopeUnification:
    """切片4（QA 判断④）：GET /installed 作用域解析统一走 _resolve_scope，
    消除与 POST 的语义漂移（session_token 恒以 token 为准，不回退 query/body）。"""

    @pytest.fixture()
    def token_client_no_repo(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user_or_api_key
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {
            "id": UID1090, "username": "agent1090", "auth_type": "session_token",
            "repo_key": "", "agent_type": "coding", "session_key": "sk-1090-norepo"}
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_installed_session_token_never_falls_back_to_query(self, token_client_no_repo):
        """session_token 的 repo_key 为空时以 token 为准（空串），不接受 query 越库。"""
        client = token_client_no_repo
        resp = client.get("/api/plugins/agent/installed",
                          params={"repo_key": "gitcode:other/repo-x"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["repo_key"] == ""


class TestSubmitDebugLogging:
    """切片5（QA 判断⑤）：市场契约调用异常不得静默吞掉——至少 debug 级日志留痕。"""

    def test_market_exception_emits_debug_log(self, monkeypatch, caplog):
        from src.domains.runtime.engine import plugin_negotiation
        import types

        def _boom(request):
            raise RuntimeError("market down")

        fake = types.ModuleType("src.api.plugin_market")
        fake.create_plugin_install_request = _boom
        monkeypatch.setitem(sys.modules, "src.api.plugin_market", fake)
        with caplog.at_level("DEBUG", logger="src.domains.runtime.engine.plugin_negotiation"):
            result = plugin_negotiation.submit_install_request(
                {"type": "plugin_install_request", "items": []})
        # 降级行为不变：继续源链（统一队列/comment），不抛异常
        assert result["channel"] in ("unified_queue", "comment")
        # 异常必须留痕：debug 级日志含失败原因
        assert any(r.levelno == 10 and "market down" in r.getMessage()
                   for r in caplog.records)


class TestPromptNegotiationGuidance:
    """切片7：协商指引落到具体端点（AC③ 可执行：报告+建议安装项走统一确认单）。"""

    def _prompt(self, monkeypatch, items):
        _patch_collect(monkeypatch, items)
        from src import prompt_builder
        return prompt_builder.build_devops_agent_prompt(dict(EVT), [], "", ["coding"],
                                                        agent_type="coding")

    def test_guidance_names_report_endpoint(self, monkeypatch):
        prompt = self._prompt(monkeypatch, [])
        assert "/api/plugins/agent/report-missing" in prompt
        assert "GRAPE_SESSION_TOKEN" in prompt
        assert "report_missing" in prompt or "report-missing" in prompt

    def test_guidance_names_refresh_endpoint(self, monkeypatch):
        prompt = self._prompt(monkeypatch, [])
        assert "/api/plugins/agent/installed" in prompt

    def test_guidance_requires_user_confirmation(self, monkeypatch):
        """补装必须经用户确认（统一确认单语义），未确认前降级继续。"""
        prompt = self._prompt(monkeypatch, [])
        assert "confirmation" in prompt.lower()
        assert "do NOT fail or block" in prompt
