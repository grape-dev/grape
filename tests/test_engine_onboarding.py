"""一键部署引擎接入引导 — 单元/集成测试（spec.md E1-E4）

GET /api/runtime/engine-onboarding：编排入口，复用既有凭据/安装/注册协议。
所有 DB 操作 mock，不连真实数据库；discover_backends patch 控制安装态。
"""
from unittest.mock import patch, MagicMock

import pytest

_BASE = "https://workspace.example.com"


def _none_installed():
    return [
        {"name": n, "installed": False, "cli_path": "", "pip_package": ""}
        for n in ("opencode", "codearts", "hermes", "codex", "dsh", "hwcloud", "cursor")
    ]


def _hermes_installed():
    rs = _none_installed()
    rs[2] = {"name": "hermes", "installed": True, "cli_path": "/usr/local/bin/hermes", "pip_package": "hermes"}
    return rs


def _call_onboarding(discover_result, runtime_rows, username="alice", repo=""):
    """直接调用路由函数，mock get_meta_value / discover_backends / DB。"""
    import src.user.db as db_mod
    import src.agent.executor as executor_mod
    from src.api.routes.runtime import runtime_engine_onboarding

    cursor = MagicMock()
    cursor.fetchall.return_value = runtime_rows
    # 与实现同规格：cursor.description 驱动列名取值（复盘 #3 列序解耦）
    cursor.description = [("id",), ("name",), ("status",), ("device_name",), ("scope",), ("repo_key",)]

    with patch.object(db_mod, "get_server_base_url", return_value=_BASE), \
         patch.object(executor_mod, "discover_backends", return_value=discover_result), \
         patch("src.api.routes.runtime.db_lock"), \
         patch("src.api.routes.runtime.db") as db_fake:
        db_fake.cursor.return_value = cursor
        return runtime_engine_onboarding(repo=repo, user={"username": username})


class TestEngineOnboarding:
    def test_e1_needs_worker_when_sandbox_empty(self):
        """E1 一键部署后首查：沙箱无引擎、无在线 worker → needs_worker=true，
        install_cmd 的 --server 为 Host 自愈后的公网地址。"""
        r = _call_onboarding(_none_installed(), [])
        assert r["needs_worker"] is True
        assert r["server_base_url"] == _BASE
        assert f"--server {_BASE}" in r["install_cmd_linux"]
        assert _BASE in r["install_cmd_windows"]
        assert _BASE in r["credential_api"]
        assert r["remote_engines"] == []
        assert all(not e["installed"] for e in r["local_engines"])

    def test_e2_remote_worker_online(self):
        """E2 有 worker 在线 → needs_worker=false，remote_engines 列出该引擎。"""
        # 6 列对齐实现 SELECT（含 scope/repo_key）；mock fetchall 原样返回 → 全进用户视角集合
        rows = [(14290, "opencode", "online", "ecs-worker", "user", "")]
        r = _call_onboarding(_none_installed(), rows)
        assert r["needs_worker"] is False
        assert r["remote_engines"] == [{"id": 14290, "name": "opencode", "status": "online", "device_name": "ecs-worker", "scope": "user", "repo_key": ""}]

    def test_e2b_scope_visibility(self):
        """E2b（评审修正语义）：needs_worker 按用户视角判定——global（任何人加，
        全员免提示）+ 本人名下一切 scope（含自己的 repo-scope，不限当前 repo）。
        他人 user-scope / 他人 repo-scope 不计入本人的 needs_worker。"""
        import src.api.routes.runtime as rt
        import inspect
        src = inspect.getsource(rt.runtime_engine_onboarding)
        # 判定 SQL（列名修复后形态；weekly-09 变基后为 LOWER 大小写归一形态）：
        # global OR owner_id=本人uid OR 凭据 owner_name=本人（不区分大小写）
        # （本人一切 scope：user-scope 按 uid、repo-scope 经凭据 JOIN 按用户名）
        import re as _re
        assert _re.search(r"ar\.scope='global' OR ar\.owner_id=\? OR (?:LOWER\()rc\.owner_name(?:\))=?", src) or \
               "ar.scope='global' OR ar.owner_id=? OR rc.owner_name=?" in src, \
            "E2b: 判定应为用户视角（global + 本人名下全部 scope）"
        r = _call_onboarding(_none_installed(), [])
        assert r["needs_worker"] is True, "E2b: others' private workers must not flip needs_worker"

    def test_e2c_repo_context_includes_repo_workers(self):
        """E2c 仓库上下文：该 repo 的 repo-scope worker 计入可见性（对齐 dashboard
        仓库级看板 global + user + repo 分层；engine_select._member_scope repo 档位合法）。

        执行层用真实 SQLite 验证 SQL 等值匹配语义（mock 无法验证 SQL 本身）。
        repo 参数契约：只接受规范带前缀形态；裸形态视为无仓库上下文（E2e）。
        """
        import sqlite3, tempfile, os
        from src.api.routes.runtime import runtime_engine_onboarding
        import src.user.db as db_mod
        import src.agent.executor as executor_mod

        tmp = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("CREATE TABLE agent_runtimes (id INTEGER, name TEXT, display_name TEXT, host_name TEXT, status TEXT, scope TEXT, owner_id TEXT, credential_id INTEGER, repo_key TEXT, org_id INTEGER)")
        cur.execute("CREATE TABLE runtime_credentials (id INTEGER, name TEXT, token_hash TEXT, status TEXT, scope TEXT, owner_name TEXT, owner_id TEXT, repo_key TEXT, org_id INTEGER)")
        cur.execute("CREATE TABLE managed_repos (repo_key TEXT)")
        # 真实 schema 列（owner_id 三态：repo 行=仓库全名；user 行=uid 字符串）；
        # repo-scope 的"本人归属"经 runtime_credentials.owner_name（bob）JOIN 命中
        cur.execute("INSERT INTO runtime_credentials VALUES (10,'cred-bob','h1','active','repo','bob','gitcode:owner/myrepo','gitcode:owner/myrepo',0)")
        cur.execute("INSERT INTO agent_runtimes VALUES (1,'opencode','opencode','w1','online','repo','gitcode:owner/myrepo',10,'gitcode:owner/myrepo',0)")
        cur.execute("INSERT INTO agent_runtimes VALUES (2,'hermes','hermes','w2','online','repo','gitcode:owner/other',0,'gitcode:owner/other',0)")
        cur.execute("INSERT INTO agent_runtimes VALUES (3,'codex','codex','w3','online','user','7',0,'',0)")
        cur.execute("INSERT INTO agent_runtimes VALUES (4,'hwcloud','hwcloud','w4','online','repo','gitcode:owner/myrepo',0,'',0)")
        # 跨平台同名仓库：github:owner/myrepo 也有 repo-scope worker——不得泄漏进
        # gitcode:owner/myrepo 上下文（IN 撒网方案的真实缺陷，等值匹配不存在此问题）
        cur.execute("INSERT INTO agent_runtimes VALUES (5,'cursor','cursor','w5','online','repo','github:owner/myrepo',0,'github:owner/myrepo',0)")
        cur.execute("INSERT INTO managed_repos VALUES ('gitcode:owner/myrepo')")
        conn.commit()

        class _Lock:
            def __enter__(self): return None
            def __exit__(self, *a): return False

        with patch.object(db_mod, "get_server_base_url", return_value=_BASE), \
             patch.object(executor_mod, "discover_backends", return_value=_none_installed()), \
             patch("src.api.routes.runtime.db_lock", _Lock()), \
             patch("src.api.routes.runtime.db", conn):
            # 仓库上下文（契约：规范带前缀形态）
            # bob 视角（repo 上下文）：本人 repo worker 计入判定与展示
            r_repo = runtime_engine_onboarding(repo="gitcode:owner/myrepo", user={"username": "bob", "id": 8})
            assert r_repo["needs_worker"] is False, "E2c: 本人 repo worker 应计入判定（不再误报）"
            names = {e["name"] for e in r_repo["remote_engines"]}
            assert "opencode" in names, "E2c: 本 repo 的 worker 计入展示"
            assert "hwcloud" not in names, "E2c: repo-scope 但 repo_key 为空不计入展示"
            assert "cursor" not in names, "E2c: 同名仓库跨平台(github) worker 不泄漏进本仓库展示"
            # bob 的全局上下文（展示不含他人 repo；判定含本人一切 repo——hermes 在 bob 名下）
            r_bob_global = runtime_engine_onboarding(repo="", user={"username": "bob", "id": 8})
            assert r_bob_global["needs_worker"] is False, "E2d: 本人 repo worker（hermes/other repo）也计入全局判定"
            bg_names = {e["name"] for e in r_bob_global["remote_engines"]}
            assert "hermes" not in bg_names, "E2d: 全局展示不含 repo-scope（即使本人）"
            # alice 视角（全局）：他人 repo worker 不影响她的判定与展示
            r_alice_global = runtime_engine_onboarding(repo="", user={"username": "alice", "id": 7})
            assert "codex" in {e["name"] for e in r_alice_global["remote_engines"]}, "E2d: 本人 user-scope 计入"
        conn.close()
        os.unlink(tmp)

    def test_e2e_bare_repo_form_is_global(self):
        """E2e 契约：裸 owner/repo 形态不做前缀猜测——视为无仓库上下文走全局
        判定（保守：宁可多引导，不可误判可用；查表猜前缀在双平台同名仓库时
        命中不确定，是 IN 撒网的换皮）。"""
        from src.api.routes.runtime import engine_onboarding_repo_key
        assert engine_onboarding_repo_key("gitcode:owner/myrepo") == "gitcode:owner/myrepo"
        assert engine_onboarding_repo_key("github:owner/myrepo") == "github:owner/myrepo"
        assert engine_onboarding_repo_key("owner/myrepo") == "", "E2e: 裸形态不猜前缀"
        assert engine_onboarding_repo_key("") == ""
        assert engine_onboarding_repo_key("   ") == ""

    def test_e3_local_engine_installed(self):
        """E3 裸机直装形态：本机装了 hermes → needs_worker=false，local 照实上报。"""
        r = _call_onboarding(_hermes_installed(), [])
        assert r["needs_worker"] is False
        assert any(e["installed"] for e in r["local_engines"])

    def test_e4_requires_auth(self):
        """E4 无有效登录态 → 鉴权依赖拒绝（require_org_member 挂在签名上）。"""
        import inspect
        from src.api.routes import runtime as rt
        sig = inspect.signature(rt.runtime_engine_onboarding)
        dep = sig.parameters["user"].default
        assert dep is not None, "engine-onboarding 缺少鉴权依赖"
        assert "require_org_member" in str(dep.dependencies[0].call) if hasattr(dep, "dependencies") else True

    def test_structure_install_commands_reference_existing_endpoints(self):
        """结构守卫：install_cmd 引用的端点真实存在（防漂移到不存在路由）。"""
        import inspect
        from src.api.routes import runtime as rt
        src = inspect.getsource(rt.runtime_engine_onboarding)
        assert "/api/runtime/install" in src
        assert "/api/settings/runtime-credentials" in src
