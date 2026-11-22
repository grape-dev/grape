"""用户/凭据/托管/技能/引擎 DB 层单元测试（src/domains/mgmt/user/db.py）。

覆盖：账户 CRUD、token、OAuth state、凭据与云主机、托管仓库与 bot token、
技能、用户仓库缓存、meta、agent_runtimes 缓存与默认引擎。全部走真实测试库（conftest 会话重置）。
"""

import json

import os

import uuid

import pytest

from unittest.mock import patch

from src.domains.mgmt.user import db as udb

from src.kernel.session_manager import db, db_lock

def _jwt_access_token(user_id, username="test"):
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

def _uid():
    return "udb_" + uuid.uuid4().hex[:8]

def _mk_user(uname=None):
    """测试用户构造——走 grape_accounts 现行权威路径（写入冻结后不再有
    user_accounts 写函数）。返回 dict 兼容旧用例的 u["id"]/u["username"] 访问。"""
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    uname = uname or _uid()
    u = create_grape_account(username=uname, email=f"{uname}@test.local",
                             password="pw123")
    if not u:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        u = get_grape_account_by_username(uname)
    return u, uname

def _cleanup_user(uname):
    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
    u = get_grape_account_by_username(uname)
    if u:
        with db_lock:
            db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (u["id"],))
            db.commit()
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()

class TestAccountsRetired:
    """user_accounts 死表收敛（D32/D105，UC26）后的等价断言。

    原 TestAccounts（create_user/verify_user/delete_user/update_password 死链路）
    按「基线可改造」口径改编：写入面函数已删除（GC4 守卫锁不许复活），
    权威账号能力由 grape_accounts 域承载（grape_account_db 自有测试域）。
    """

    _RETIRED = (
        "create_user", "verify_user", "delete_user", "update_password",
        "increment_token_version", "get_token_version", "link_gitcode_user",
        "link_github_user", "save_user_account", "delete_user_account",
    )

    def test_deprecated_write_funcs_not_resurrected(self):
        for fn in self._RETIRED:
            assert not hasattr(udb, fn), f"{fn} 复活（user_accounts 写入面冻结铁律）"

    def test_authoritative_account_only_in_grape_accounts(self):
        """账号权威表是 grape_accounts；user_accounts 只留历史读，不再承载新账号。"""
        u, uname = _mk_user()
        try:
            assert u and u["username"] == uname
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
            assert get_grape_account_by_username(uname)["id"] == u["id"]
            # 旧表读函数已随 user_accounts 清退删除（2026-11-22）：不再"查了返回空"，
            # 而是根本不存在 —— 防止有人误以为它仍是可用读入口。
            assert not hasattr(udb, "get_user")
        finally:
            _cleanup_user(uname)

# ====== 凭据 / 云主机 ======



    def test_external_identity_binding(self):
        """gitcode 绑定等价用例：Issue #1588 后绑定关系存 external_identities"""
        u, uname = _mk_user()
        try:
            from src.domains.mgmt.auth.grape_account_db import (
                create_external_identity, get_external_identity, delete_external_identity)
            assert get_external_identity("gitcode", "nonexistent_gid") is None
            assert create_external_identity(u["id"], "gitcode", "gid_123",
                                            access_token="at", refresh_token="rt") is True
            got = get_external_identity("gitcode", "gid_123")
            assert got and str(got["grape_user_id"]) == str(u["id"])
            delete_external_identity(u["id"], "gitcode")
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
            # D32-补丁：get_credentials 现在从 user_credentials 表 + external_identities 读
            udb.set_credential(u["id"], "gitcode", {"token": "tok_1"})
            udb.set_credential(u["id"], "feishu", {"app_id": "cli_f"})
            udb.set_credential(u["id"], "huawei_cloud", {"ak": "ak"})
            udb.set_credential(u["id"], "codearts", {"ak": "ck"})
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
            # D38 后 webhook 重建走适配器端口（真实 HTTP）——mock 返回 id=1
            from unittest.mock import patch as _patch
            with _patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="1"):
                _bt_result = udb.update_managed_repo_bot_token(rk, "new_bt")
            assert _bt_result.get("ok") is True
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
            # 全局兜底（D22 后 TokenResolver 全局策略走 meta key 来源链：
            # global_bot_<provider>_token ——写 meta 实测，mock 旧接缝失效）
            udb.set_meta_value("global_bot_gitcode_token", "global_bt")
            try:
                assert udb.get_gitcode_token(repo_key="gitcode:org/no-repo") == "global_bt"
            finally:
                udb.set_meta_value("global_bot_gitcode_token", "")
            # 空 repo + 无 user → ""
            assert udb.get_gitcode_token() == ""
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
                db.commit()

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

class TestMetaAccounts:
    def test_meta_roundtrip(self):
        key = "udb_key_" + uuid.uuid4().hex[:6]
        assert udb.get_meta_value(key) == ""
        udb.set_meta_value(key, "v1")
        assert udb.get_meta_value(key) == "v1"
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key=?", (key,))
            db.commit()

    # test_account_extension 已删除（2026-11-07 写入冻结）——
    # save_user_account/delete_user_account/get_user_account/get_all_user_accounts
    # 是 user_accounts 死表读写函数，账户扩展字段的现行覆盖在
    # grape_accounts 体系（metadata_json 列）。

    def test_get_all_bot_usernames(self):
        rk = "gitcode:org/udb_bot_%s" % uuid.uuid4().hex[:4]
        udb.add_managed_repo(rk, "org", "b", bot_username="repo_bot")
        try:
            # 全局 bot 用户名走统一入口（DB→文件来源链，Issue #345）——mock 消除文件环境依赖
            from unittest.mock import patch as _patch
            with _patch("src.domains.mgmt.user.db.get_global_bot_usernames", return_value=["grape_dev_bot"]):
                names = udb.get_all_bot_usernames()
            assert "repo_bot" in names
            assert "grape_dev_bot" in names  # 全局 bot（来源链统一入口）
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
                db.commit()



    def test_account_profile_extension(self):
        """账户资料扩展等价用例：Issue #1588 后走 grape_accounts.update_profile"""
        u, uname = _mk_user()
        try:
            from src.domains.mgmt.auth.grape_account_db import (
                get_grape_account_by_username, get_grape_account_by_id, update_profile)
            acc = get_grape_account_by_username(uname)
            assert acc and acc["username"] == uname
            assert get_grape_account_by_username("nonexistent_user") is None
            assert update_profile(u["id"], nickname="n2") is True
            assert get_grape_account_by_id(u["id"])["nickname"] == "n2"
            assert get_grape_account_by_id("nonexistent_id") is None
        finally:
            _cleanup_user(uname)

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
            with patch("src.kernel.config.read_server_config", return_value={}):
                assert udb.get_webhook_url() == "https://srv/webhook/gitcode"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_strips_suffix(self):
        self._reset_meta("webhook_url", "server_base_url")
        udb.set_meta_value("webhook_url", "https://srv/webhook/gitcode")
        try:
            with patch("src.kernel.config.read_server_config", return_value={}):
                assert udb.get_server_base_url() == "https://srv"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_meta_direct(self):
        self._reset_meta("webhook_url", "server_base_url")
        udb.set_meta_value("server_base_url", "https://base/")
        try:
            with patch("src.kernel.config.read_server_config", return_value={}):
                assert udb.get_server_base_url() == "https://base"
        finally:
            self._reset_meta("webhook_url", "server_base_url")

    def test_server_base_url_default(self):
        self._reset_meta("webhook_url", "server_base_url")
        with patch("src.kernel.config.read_server_config", return_value={}):
            # 默认基址 = http://localhost:{GRAPE_PORT}，GRAPE_PORT 默认 3000
            assert udb.get_server_base_url() == "http://localhost:3000"


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
            with patch("src.domains.runtime.agents.executor.discover_backends", return_value=[
                    {"name": "opencode", "installed": True, "cli_path": "/x"}]):
                installed, cli = udb._local_engine_info("opencode")
                assert installed is True and cli == "/x"
                # 已知引擎 → 创建默认 runtime
                rid = udb.ensure_default_runtime("opencode")
                assert rid and rid > 0
                # 已存在 → 复用
                assert udb.ensure_default_runtime("opencode") == rid
            # 未知引擎 → 拒绝创建
            with patch("src.domains.runtime.agents.executor.discover_backends", return_value=[]):
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

from src.domains.mgmt.auth import grape_account_db as gad

"""用户/凭据/托管/技能/引擎 DB 层单元测试（src/domains/mgmt/domains/mgmt/user/db.py）。

覆盖：OAuth state、凭据与云主机、托管仓库与 bot token、技能、用户仓库缓存、
meta、agent_runtimes 缓存与默认引擎。全部走真实测试库（conftest 会话重置）。

2026-11-07 user_accounts 写入冻结（铁律：废弃表零写入）：
- 原 TestAccounts（create_user/verify_user/delete_user/update_password 死链路
  用例）已删除——替代路径 grape_accounts 体系有自己的测试域（grape_account_db）
- 测试辅助函数迁移到 grape_accounts（现行权威账号表）：
  _mk_user → grape_account_db.create_grape_account
  _cleanup_user → grape_accounts 硬删（测试库清理，生产走 soft_delete_account）
- link_gitcode_user 用例已删除——替代 create_external_identity 的覆盖在
  tests/test_issue_372_*.py（身份识别域）
"""
