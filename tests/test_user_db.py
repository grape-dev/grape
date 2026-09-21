"""用户/凭据/托管/技能/引擎 DB 层单元测试（src/user/db.py）。

覆盖：账户 CRUD、token、OAuth state、凭据与云主机、托管仓库与 bot token、
技能、用户仓库缓存、meta、agent_runtimes 缓存与默认引擎。全部走真实测试库（conftest 会话重置）。
"""
import json
import os
import uuid

import pytest
from unittest.mock import patch

from src.user import db as udb
from src.session_manager import db, db_lock

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _uid():
    return "udb_" + uuid.uuid4().hex[:8]


def _mk_user(uname=None):
    uname = uname or _uid()
    u = udb.create_user(uname, "pw123")
    if not u:
        u = udb.get_user_by_username(uname)
    return u, uname


def _cleanup_user(uname):
    u = udb.get_user_by_username(uname)
    if u:
        udb.delete_user(u["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


# ====== 账户 / 认证 ======


class TestAccounts:
    def test_create_and_get(self):
        u, uname = _mk_user()
        try:
            got = udb.get_user(u["id"])
            assert got["username"] == uname
            byname = udb.get_user_by_username(uname)
            assert byname["id"] == u["id"]
            assert udb.list_users()  # 非空
        finally:
            _cleanup_user(uname)

    def test_create_duplicate_returns_none(self):
        u, uname = _mk_user()
        try:
            assert udb.create_user(uname, "pw") is None
        finally:
            _cleanup_user(uname)

    def test_delete_user(self):
        u, uname = _mk_user()
        assert udb.delete_user(u["id"]) is True
        assert udb.delete_user(u["id"]) is False  # 已删除
        assert udb.get_user(u["id"]) is None

    def test_update_password(self):
        u, uname = _mk_user()
        try:
            assert udb.update_password(u["id"], "newpass") is True
            assert udb.update_password(999999, "x") is False
            assert udb.verify_user(uname, "newpass") is not None
        finally:
            _cleanup_user(uname)

    def test_verify_user(self):
        u, uname = _mk_user()
        try:
            assert udb.verify_user(uname, "pw123") is not None
            assert udb.verify_user(uname, "wrong") is None
            assert udb.verify_user("nobody_exists", "pw123") is None
        finally:
            _cleanup_user(uname)

    def test_token_flow(self):
        u, uname = _mk_user()
        try:
            # JWT 无状态——create_access_token + verify_access_token
            from src.api.jwt_utils import create_access_token, verify_access_token
            tok = create_access_token(u["id"], u["username"])
            payload = verify_access_token(tok)
            assert payload is not None
            assert int(payload["sub"]) == u["id"]
            # 坏 token 验证失败
            assert verify_access_token("bad") is None
        finally:
            _cleanup_user(uname)


class TestOAuth:
    def test_save_validate_flow(self):
        state = "test-state-" + uuid.uuid4().hex[:8]
        udb.save_oauth_state(state, "gitcode", {"k": 1})
        assert udb.validate_oauth_state(state, "gitcode") == {"k": 1}
        # 消耗后失效
        assert udb.validate_oauth_state(state, "gitcode") is None

    def test_validate_provider_mismatch(self):
        state = "ts-" + uuid.uuid4().hex[:8]
        udb.save_oauth_state(state, "gitcode", {})
        assert udb.validate_oauth_state(state, "feishu") is None

    def test_cleanup_expired(self):
        udb._cleanup_expired_oauth_states()  # 不应抛错

    def test_get_user_by_gitcode_id(self):
        u, uname = _mk_user()
        try:
            assert udb.get_user_by_gitcode_id("nonexistent_gid") is None
            assert udb.link_gitcode_user(u["id"], "gid_123", "at", "rt") is True
            got = udb.get_user_by_gitcode_id("gid_123")
            assert got and got["id"] == u["id"]
        finally:
            _cleanup_user(uname)


# ====== 凭据 / 云主机 ======


class TestCredentials:
    def _user(self):
        u, uname = _mk_user()
        return u, uname

    def test_set_get_credential(self):
        u, uname = self._user()
        try:
            assert udb.set_credential(u["id"], "feishu", {"app_id": "a1"}) is True
            cred = udb.get_credential(u["id"], "feishu")
            assert cred == {"app_id": "a1"}
            assert udb.get_credential(u["id"], "nope") is None
            assert udb.delete_credential(u["id"], "feishu") is True
            assert udb.get_credential(u["id"], "feishu") is None
        finally:
            _cleanup_user(uname)

    def test_get_credentials_from_account_fields(self):
        u, uname = self._user()
        try:
            acc = udb.get_user_account(uname) or {}
            acc["gitcode_api_token"] = "tok_1"
            acc["feishu_app_id"] = "cli_f"
            acc["huawei_cloud_ak"] = "ak"
            acc["CODEARTS_CLI_AK"] = "ck"
            udb.save_user_account(uname, acc)
            creds = udb.get_credentials(u["id"])
            providers = {c["provider"] for c in creds}
            assert {"gitcode", "feishu", "huawei_cloud", "codearts"} <= providers
            assert udb.get_user_gitcode_token(u["id"]) == "tok_1"
            assert udb.get_user_gitcode_token(0) == ""
            assert udb.get_user_gitcode_token(-1) == ""
        finally:
            _cleanup_user(uname)

    def test_get_credentials_unknown_user(self):
        assert udb.get_credentials(999999) == []


# ====== 托管仓库 ======


class TestManagedRepos:
    def _rk(self):
        return "gitcode:org/udb-%s" % uuid.uuid4().hex[:6]

    def _rk_full(self):
        # repo_key 与 owner/name 一致，保证 get_bot_token_by_repo 可命中
        return "gitcode:org/udb_repo_%s" % uuid.uuid4().hex[:4]

    def test_add_and_scene(self):
        rk = self._rk_full()
        repo_name = rk.split("/")[-1]
        assert udb.add_managed_repo(rk, "org", repo_name, webhook_id=9, bot_gitcode_token="btok",
                                    gitcode_webhook_token="wtok", bot_username="grape_bot") is True
        assert udb.get_repo_scene(rk) == ""
        udb.set_repo_scene(rk, "skill-dev")
        assert udb.get_repo_scene(rk) == "skill-dev"
        assert udb.get_repo_scene("gitcode:nope/nope") == ""
        # token helpers
        assert udb.get_bot_token_by_repo("org", repo_name) == "btok"
        assert udb.get_repo_bot_token_no_global(rk) == "btok"
        assert udb.get_webhook_token_by_repo("org/" + repo_name) == "wtok"
        assert udb.get_all_managed_repos()
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.commit()

    def test_update_and_remove(self):
        rk = self._rk_full()
        repo_name = rk.split("/")[-1]
        udb.add_managed_repo(rk, "org", repo_name, webhook_id=1)
        u, uname = _mk_user()
        try:
            with db_lock:
                c = db.cursor()
                c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,1,1)",
                          (u["id"], rk, "org", repo_name))
                db.commit()
            assert udb.update_managed_repo_admin(u["id"], rk, False) is True
            assert udb.update_managed_repo_bot_token(rk, "new_bt") is True
            assert udb.update_managed_repo_bot_username(rk, "grape_bot") is True
            assert udb.get_bot_token_by_repo("org", repo_name) == "new_bt"
            removed = udb.remove_managed_repo(rk)
            assert removed and removed["webhook_id"] == 1
            assert udb.remove_managed_repo(rk) is None  # 已删除
        finally:
            _cleanup_user(uname)
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
                db.execute("DELETE FROM user_repos WHERE repo_key=?", (rk,))
                db.commit()

    def test_gitcode_token_resolution(self):
        rk = self._rk()
        udb.add_managed_repo(rk, "org", "repo-z", bot_gitcode_token="repo_bt")
        try:
            # 仓库级 token
            assert udb.get_gitcode_token(repo_key=rk) == "repo_bt"
            # 全局兜底
            with patch("src.user.db._get_global_bot_token", return_value="global_bt"):
                assert udb.get_gitcode_token(repo_key="gitcode:org/no-repo") == "global_bt"
            # 空 repo + 无 user → ""
            assert udb.get_gitcode_token() == ""
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
                db.commit()


# ====== 技能 ======


class TestSkills:
    def test_skill_by_id_and_resolve(self):
        u, uname = _mk_user()
        try:
            assert udb.get_skill_by_id(None) is None
            assert udb.get_skill_by_id(999999) is None
            udb.create_skill(u["id"], "udbskill_%s" % uuid.uuid4().hex[:4], "issue", "desc")
            skill = udb.list_skills(u["id"])[0]
            got = udb.get_skill_by_id(skill["id"])
            assert got and got["skill_name"] == skill["skill_name"]
            # 路径解析（official/user/repo）
            assert udb.get_skill_path(skill["id"]).endswith("SKILL.md")
            assert udb.get_skill_path(999999) == ""
        finally:
            _cleanup_user(uname)

    def test_resolve_skill_id(self):
        assert udb.resolve_skill_id("") == 0
        assert udb.resolve_skill_id("nonexistent_skill_xyz") == 0
        u, uname = _mk_user()
        try:
            sname = "res_%s" % uuid.uuid4().hex[:4]
            udb.create_skill(u["id"], sname, "vod")
            # user scope: 用 user_id 参数查
            sid = udb.resolve_skill_id(sname, user_id=str(u["id"]))
            assert sid and sid > 0
            # 旧 owner_id 兼容（非空时作为 repo_key 回退，user scope 查不到）
            assert udb.resolve_skill_id(sname, owner_id=str(u["id"])) == 0
        finally:
            _cleanup_user(uname)

    def test_skill_md_save_get_delete(self):
        u, uname = _mk_user()
        try:
            sname = "md_%s" % uuid.uuid4().hex[:4]
            assert udb.save_skill_md(u["id"], sname, "# hello") is True
            assert udb.get_skill_md(u["id"], sname) == "# hello"
            assert udb.get_skill_md(u["id"], "missing_skill") is None
            assert udb.create_skill(u["id"], sname, "qa") is True
            assert udb.delete_skill(u["id"], sname) is True
            assert udb.delete_skill(u["id"], sname) is False
        finally:
            _cleanup_user(uname)


# ====== 用户仓库缓存 ======


class TestUserRepos:
    def test_save_get_clear(self):
        u, uname = _mk_user()
        try:
            udb.save_user_repos(u["id"], [
                {"full_name": "org/a", "owner": "org", "name": "a", "is_admin": True},
                {"full_name": "org/b", "owner": "org", "name": "b", "is_admin": False},
            ])
            repos = udb.get_user_repos(u["id"])
            assert len(repos) == 2
            assert repos[0]["full_name"] == "org/a"
            assert udb.has_user_repos(u["id"]) is True
            udb.mark_user_repo_join(u["id"], "gitcode:org/b", is_join=True, is_admin=True)
            # 插入不存在行
            udb.mark_user_repo_join(u["id"], "gitcode:org/c", is_join=True)
            assert len(udb.get_user_repos(u["id"])) == 3
            udb.clear_user_repos(u["id"])
            assert udb.has_user_repos(u["id"]) is False
        finally:
            _cleanup_user(uname)

    def test_synced_at_empty(self):
        u, uname = _mk_user()
        try:
            assert udb.get_user_repos_synced_at(u["id"]) == ""
        finally:
            _cleanup_user(uname)


# ====== meta / 账户扩展 ======


class TestMetaAccounts:
    def test_meta_roundtrip(self):
        key = "udb_key_" + uuid.uuid4().hex[:6]
        assert udb.get_meta_value(key) == ""
        udb.set_meta_value(key, "v1")
        assert udb.get_meta_value(key) == "v1"
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key=?", (key,))
            db.commit()

    def test_account_extension(self):
        uname = _uid()
        udb.save_user_account(uname, {"nickname": "n1"})
        try:
            acc = udb.get_user_account(uname)
            assert acc and acc["name"] == uname and acc["nickname"] == "n1"
            assert acc["bot_usernames"] == []  # USER_ACCOUNT_FIELDS 不含该列，默认空
            assert udb.get_user_account("nonexistent_user") is None
            # 更新（保留 id）
            udb.save_user_account(uname, {"nickname": "n2"})
            acc2 = udb.get_user_account(uname)
            assert acc2["nickname"] == "n2"
            assert udb.get_all_user_accounts()
            assert udb.delete_user_account(uname) is True
            assert udb.delete_user_account(uname) is False
        finally:
            _cleanup_user(uname)

    def test_get_all_bot_usernames(self):
        rk = "gitcode:org/udb_bot_%s" % uuid.uuid4().hex[:4]
        udb.add_managed_repo(rk, "org", "b", bot_username="repo_bot")
        try:
            names = udb.get_all_bot_usernames()
            assert "repo_bot" in names
            assert "grape_bot" in names  # 全局 bot（configs/global-bot-token.json）
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
                db.commit()


class TestServerUrls:
    def _reset_meta(self, *keys):
        with db_lock:
            c = db.cursor()
            for k in keys:
                c.execute("DELETE FROM meta WHERE key=?", (k,))
            db.commit()

    def test_webhook_url_from_meta(self):
        self._reset_meta("webhook_url", "server_base_url")
        udb.set_meta_value("webhook_url", "https://srv/webhook/gitcode")
        try:
            with patch("src.config.read_server_config", return_value={}):
                assert udb.get_webhook_url() == "https://srv/webhook/gitcode"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_strips_suffix(self):
        self._reset_meta("webhook_url", "server_base_url")
        udb.set_meta_value("webhook_url", "https://srv/webhook/gitcode")
        try:
            with patch("src.config.read_server_config", return_value={}):
                assert udb.get_server_base_url() == "https://srv"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_meta_direct(self):
        self._reset_meta("webhook_url", "server_base_url")
        udb.set_meta_value("server_base_url", "https://base/")
        try:
            with patch("src.config.read_server_config", return_value={}):
                assert udb.get_server_base_url() == "https://base"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_default(self):
        self._reset_meta("webhook_url", "server_base_url")
        with patch("src.config.read_server_config", return_value={}):
            assert udb.get_server_base_url() == "http://localhost:8080"


class TestAliases:
    def test_aliases_delegate(self):
        assert udb.save_backend_agents([]) is None  # 空列表不抛错
        assert udb.get_backend_agents("global") == udb.get_agent_runtimes("global")
        assert udb.get_backend_id_by_name("nonexistent_engine") == udb.get_runtime_id_by_name("nonexistent_engine")
        assert udb.ensure_default_backend("opencode") == udb.ensure_default_runtime("opencode")
        # age 是时间差，两次计算略有差异，用 approx
        a1, a2 = udb.get_backend_agents_age(), udb.get_agent_runtimes_age()
        assert a1 is None and a2 is None or abs(a1 - a2) < 1
        assert udb.get_agent_bot_token("") == udb.get_gitcode_token(repo_key="")


# ====== agent_runtimes 缓存 ======


class TestAgentRuntimes:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'udb_rt%'")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'udb_rt%'")
            db.commit()

    def test_save_and_get(self):
        self._clean()
        try:
            udb.save_agent_runtimes([
                {"name": "udb_rt_opencode", "display_name": "Opencode", "installed": True,
                 "cli_path": "/usr/bin/opencode", "version": "1.0", "healthy": True},
            ])
            rows = udb.get_agent_runtimes("global")
            assert any(r["name"] == "udb_rt_opencode" and r["installed"] for r in rows)
            # scope=None 全量
            assert udb.get_agent_runtimes(None)
            # 未安装则跳过（该次调用会清理非名单的本机行，故放在最后断言）
            udb.save_agent_runtimes([{"name": "udb_rt_ghost", "installed": False}])
            assert not any(r["name"] == "udb_rt_ghost" for r in udb.get_agent_runtimes("global"))
        finally:
            self._clean()

    def test_save_updates_existing(self):
        self._clean()
        try:
            udb.save_agent_runtimes([{"name": "udb_rt_x", "installed": True, "version": "v1", "healthy": True}])
            id1 = udb.get_runtime_id_by_name("udb_rt_x")
            udb.save_agent_runtimes([{"name": "udb_rt_x", "installed": True, "version": "v2", "healthy": True}])
            id2 = udb.get_runtime_id_by_name("udb_rt_x")
            assert id1 == id2  # id 稳定
            assert udb.get_runtime_id_by_name("missing_rt") is None
            assert udb.get_local_direct_runtime_id("") is None
        finally:
            self._clean()

    def test_is_known_engine(self):
        assert udb.is_known_engine_name("opencode") is True
        assert udb.is_known_engine_name("") is False
        assert udb.is_known_engine_name("not_a_real_engine_xyz") is False

    def test_local_engine_info_and_ensure_default(self):
        self._clean()
        try:
            with patch("src.agent.executor.discover_backends", return_value=[
                    {"name": "opencode", "installed": True, "cli_path": "/x"}]):
                installed, cli = udb._local_engine_info("opencode")
                assert installed is True and cli == "/x"
                # 已知引擎 → 创建默认 runtime
                rid = udb.ensure_default_runtime("opencode")
                assert rid and rid > 0
                # 已存在 → 复用
                assert udb.ensure_default_runtime("opencode") == rid
            # 未知引擎 → 拒绝创建
            with patch("src.agent.executor.discover_backends", return_value=[]):
                assert udb.ensure_default_runtime("ghost_engine") is None
        finally:
            self._clean()

    def test_runtimes_age(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes (name, display_name, installed, healthy, scope, owner_id, updated_at) "
                      "VALUES ('udb_rt_age','Age',1,1,'global','',datetime('now'))")
            db.commit()
        try:
            age = udb.get_agent_runtimes_age()
            assert age is not None and age >= 0
        finally:
            self._clean()
