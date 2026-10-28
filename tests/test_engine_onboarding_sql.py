# -*- coding: utf-8 -*-
"""engine-onboarding 判定 SQL 的真实 SQLite 回归测试（列名锁定）。

背景（2026-10-28 生产实测发现）：判定 SQL 曾误用 runtime_credentials 的列名
（device_name/owner_name）查询 agent_runtimes——SQLite 抛 no such column 被
except 兜底吞掉，remote_engines 恒空，导致有在线 worker 的部署也被误报
needs_worker=true（用户有引擎却被告知"没有可用引擎"）。此前的 mock 单测自建
了一张带同样错误列名的假表——测试与实现共享同一错误假设，绿灯毫无意义。

本测试直接在 conftest 初始化的真实测试库（真实 db_setup DDL）上插行验证，
锁定"SQL 列名与真实 schema 一致"这一类错误：列名再漂移，INSERT/查询立即红。
必测反向用例：有引擎 → 不误报。
"""
import pytest


def _seed(conn, rows):
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_runtimes")
    cur.execute("DELETE FROM runtime_credentials")
    # runtime_credentials 真实列：owner_name 为用户名（JOIN 覆盖 repo-scope 归属）
    for i, (uname, scope, repo_key) in enumerate([("alice", "user", ""),
                                                   ("bob", "repo", "gitcode:owner/myrepo"),
                                                   ("carol", "user", "")], start=101):
        cur.execute(
            "INSERT INTO runtime_credentials (id, name, token_hash, scope, owner_name, owner_id, repo_key, org_id) "
            "VALUES (?,?,?,?,?,?,?,?)", (i, f"cred-{uname}", f"hash-{i}", scope, uname, "", repo_key if scope == "repo" else "", 0))
    # agent_runtimes 真实列：owner_id 三态（uid 字符串 / 仓库全名 / ''）
    for name, scope, owner_id, cred_id, repo_key, org_id, status in rows:
        cur.execute(
            "INSERT INTO agent_runtimes (name, display_name, host_name, scope, owner_id, "
            "credential_id, repo_key, org_id, status, installed) VALUES (?,?,?,?,?,?,?,?,?,0)",
            (name, name, name + "-host", scope, owner_id, cred_id, repo_key, org_id, status))
    conn.commit()


def _call(repo="", user=None):
    from src.api.routes.runtime import runtime_engine_onboarding
    u = user or {"username": "dave", "id": 999, "org_id": 0}
    return runtime_engine_onboarding(repo=repo, user=u)


@pytest.fixture()
def real_db(monkeypatch):
    """使用 conftest 已初始化的真实测试库连接（列名=真实 DDL）。"""
    from src.session_manager import db, db_lock
    import src.user.db as db_mod
    import src.agent.executor as executor_mod
    from unittest.mock import patch
    _seed(db, [])
    with patch.object(db_mod, "get_server_base_url", return_value="https://test-base"), \
         patch.object(executor_mod, "discover_backends",
                      return_value=[{"name": n, "installed": False, "cli_path": "", "pip_package": ""}
                                    for n in ("opencode", "hermes")]):
        yield db


class TestOnboardingSqlColumns:
    def test_no_engines_needs_worker_true(self, real_db):
        """正向用例：无任何在线引擎 → needs_worker=true（引导该出现）。"""
        _seed(real_db, [])
        r = _call()
        assert r["needs_worker"] is True
        assert r["remote_engines"] == []

    def test_online_global_engine_no_misreport(self, real_db):
        """反向用例（本次生产 bug 的完全体）：有 online global 引擎
        → needs_worker 必须为 false——列名错误时此查询抛异常被吞、误报 true。"""
        _seed(real_db, [("opencode", "global", "", 0, "", 0, "online")])
        r = _call()
        assert r["needs_worker"] is False, "有在线引擎仍误报 needs_worker（列名/JOIN 错误）"
        assert len(r["remote_engines"]) == 1
        assert r["remote_engines"][0]["device_name"] == "opencode-host"

    def test_own_user_scope_engine_counts(self, real_db):
        """本人 user-scope（owner_id=str(uid)）→ 计入，免提示。"""
        _seed(real_db, [("hermes", "user", "999", 101, "", 0, "online")])
        r = _call(user={"username": "dave", "id": 999, "org_id": 0})
        assert r["needs_worker"] is False

    def test_own_repo_worker_via_credentials_join(self, real_db):
        """本人 repo-scope（owner_id=仓库全名，凭据 owner_name=bob）
        → JOIN runtime_credentials 命中，bob 免提示；dave 不免。"""
        _seed(real_db, [("opencode", "repo", "gitcode:owner/myrepo", 102, "gitcode:owner/myrepo", 0, "online")])
        r_bob = _call(repo="gitcode:owner/myrepo", user={"username": "bob", "id": 7, "org_id": 0})
        assert r_bob["needs_worker"] is False, "本人 repo worker（经凭据 JOIN）应计入"
        r_dave = _call(repo="gitcode:owner/myrepo", user={"username": "dave", "id": 999, "org_id": 0})
        assert r_dave["needs_worker"] is True, "他人 repo worker 不计入本人判定"

    def test_others_private_engine_not_shared(self, real_db):
        """他人 user-scope 引擎（owner_id=别人的 uid）→ 不计入本人。"""
        _seed(real_db, [("codex", "user", "777", 101, "", 0, "online")])
        r = _call(user={"username": "dave", "id": 999, "org_id": 0})
        assert r["needs_worker"] is True

    def test_org_isolation(self, real_db):
        """org 隔离：他组织的 global 引擎对本组织用户不可见（不计数、不泄露）。"""
        _seed(real_db, [("opencode", "global", "", 0, "", 5, "online")])
        r = _call(user={"username": "dave", "id": 999, "org_id": 3})
        assert r["needs_worker"] is True, "跨组织 global 引擎不应免本组织提示"
        assert r["remote_engines"] == [], "跨组织引擎不得出现在展示列表"

    def test_offline_engine_does_not_count(self, real_db):
        """离线引擎（status != online）不计入——只有在线才算可用。"""
        _seed(real_db, [("opencode", "global", "", 0, "", 0, "offline")])
        r = _call()
        assert r["needs_worker"] is True

    def test_username_join_case_insensitive(self, real_db):
        """用户名 JOIN 大小写不敏感（对齐库内 LOWER(username) 惯例）：
        凭据 owner_name='Alice'，查询用户名 'alice' → 仍命中（大小写差异不造成误报）。"""
        from unittest.mock import patch
        import src.user.db as db_mod
        import src.agent.executor as executor_mod
        cur = real_db.cursor()
        cur.execute("DELETE FROM agent_runtimes"); cur.execute("DELETE FROM runtime_credentials")
        cur.execute("INSERT INTO runtime_credentials (id, name, token_hash, scope, owner_name, owner_id, repo_key, org_id) VALUES (301,'cred-x','hx','repo','Alice','','gitcode:owner/r',0)")
        cur.execute("INSERT INTO agent_runtimes (name, display_name, host_name, scope, owner_id, credential_id, repo_key, org_id, status, installed) VALUES ('opencode','opencode','h','repo','gitcode:owner/r',301,'gitcode:owner/r',0,'online',0)")
        real_db.commit()
        with patch.object(db_mod, "get_server_base_url", return_value="https://t"), \
             patch.object(executor_mod, "discover_backends", return_value=[]):
            from src.api.routes.runtime import runtime_engine_onboarding
            r = runtime_engine_onboarding(repo="gitcode:owner/r", user={"username": "alice", "id": 55, "org_id": 0})
        assert r["needs_worker"] is False, "凭据 owner_name 大小写差异不应漏命中（误报）"
