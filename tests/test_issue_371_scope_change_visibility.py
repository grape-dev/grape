"""Issue #371：个人引擎改为仓库级后在 Workers 设备页不可见。

根因（三处叠加）：
1. change_credential_scope 切换 scope 时未同步 runtime_credentials.org_id
   （保留创建时绑定的组织 org_id），非个人组织视图按 org_id 严格过滤把 repo scope
   凭据/引擎滤除（#338 同源口径的镜像场景：#338 是个人组织视图漏豁免，本 Issue 是
   非个人组织视图漏 repo scope 豁免）。
2. change_credential_scope line 1288 无条件清 owner_id=''，把 repo scope 刚写入的
   owner_id=repo_key 抹掉（repo scope 设备 owner_id 恒空，_visible_credential 回退到
   crepo_key 判定，多一层脏数据）。
3. get_agent_runtimes / get_runtime_devices 非个人组织严格隔离分支无 repo scope 豁免
   （#338 已为个人组织视图补了 repo scope 豁免，非个人组织视图漏了）。

修正后语义：
- 切换 scope 时同步 org_id：repo scope → 仓库所属组织 org_id（managed_repos.org_id 权威）；
  user scope → 0（个人 worker 跨组织可见性由 _visible_credential owner 判定收敛）。
- repo scope 保留 owner_id=repo_key（不再被无条件清空）。
- 非个人组织严格隔离分支为 repo scope 增加与 #338 同口径豁免（可见性由 _visible_credential 收敛）。

回归覆盖：
- #338：个人组织视图个人归属 worker 设备可见不回退
- #277：仓库隔离不越权（非仓库成员看不到 repo scope 设备）
"""
import os
import sys
import hashlib

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_issue_371.db"))
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
# 清除可能被 .env 设置的 MySQL 连接参数（与 conftest.py 一致，防止误连生产库）
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
    os.environ.pop(_k, None)

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

# 测试组织 ID（避开生产组织 ID 段）
_ORG_PERSONAL = 937101      # songspace 个人组织
_ORG_COMPANY = 937102       # 社区博客团队（非个人组织，Issue #371 场景）
_ORG_COMPANY_OTHER = 937103  # 另一家非个人组织（严格隔离对照）
_ORG_REPO_OWNER = 937104    # 仓库所属组织（repo_key 绑定的 managed_repos.org_id）

_USER_ID = 77
_USER_NAME = "songspace"
_REPO_FULL = "songspace/blog-team"     # 仓库全名（无前缀）
_REPO_KEY = "gitcode:songspace/blog-team"


@pytest.fixture(autouse=True)
def _seed():
    """每个测试前清理 + 种入基础数据。"""
    with db_lock:
        c = db.cursor()
        # 清理测试数据（只删 test_371 前缀，遵循「DELETE 必须带 WHERE 只删测试数据」约束）
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't371-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't371-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?,?,?)",
                  (_ORG_PERSONAL, _ORG_COMPANY, _ORG_COMPANY_OTHER, _ORG_REPO_OWNER, 0))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        # 种入组织
        for oid, slug, owner, personal in (
            (_ORG_PERSONAL, "t371-songspace", _USER_ID, 1),
            (_ORG_COMPANY, "t371-company", 1, 0),
            (_ORG_COMPANY_OTHER, "t371-company-b", 1, 0),
            (_ORG_REPO_OWNER, "t371-repo-org", 1, 0),
        ):
            c.execute(
                "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,?)",
                (oid, slug, slug, owner, personal))
        # 种入用户
        c.execute(
            "INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)",
            (_USER_ID, _USER_NAME))
        # 种入 managed_repos：仓库所属组织 = _ORG_REPO_OWNER（与 _ORG_COMPANY 不同，
        # 模拟 Issue #371 场景：用户在社区博客团队组织下操作，但仓库归属另一个组织）
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) "
            "VALUES (?,?,?,?,?)",
            (_REPO_KEY, _USER_NAME, "blog-team", _REPO_FULL, _ORG_REPO_OWNER))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't371-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't371-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?,?,?)",
                  (_ORG_PERSONAL, _ORG_COMPANY, _ORG_COMPANY_OTHER, _ORG_REPO_OWNER, 0))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        db.commit()


def _user_in_company():
    """用户在社区博客团队（非个人组织）上下文。"""
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_COMPANY}


def _user_in_personal():
    """用户在个人组织上下文。"""
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_PERSONAL}


def _create_user_scope_cred(name="t371-worker-user"):
    """创建一个 user scope 凭据（org_id 绑定为公司组织，模拟创建时所在组织上下文）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, owner_id, org_id, status) "
            "VALUES (?,?,?,?,?,?,?, 'active')",
            (hashlib.sha256(name.encode()).hexdigest(), name, name,
             "user", _USER_NAME, "", _ORG_COMPANY))
        cid = c.lastrowid
        # 种入引擎行（scope=user, org_id=公司组织）
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'user','',?,'online')",
            (cid, "t371-opencode", "t371-Opencode", _ORG_COMPANY))
        db.commit()
        return cid


def _create_repo_scope_cred(name="t371-worker-repo", org_id=None):
    """创建一个 repo scope 凭据（模拟切换后的状态）。

    org_id 默认用 _ORG_REPO_OWNER（仓库所属组织），模拟「切换入口已同步 org_id」的修正后状态。
    """
    if org_id is None:
        org_id = _ORG_REPO_OWNER
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, owner_id, repo_key, org_id, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'active')",
            (hashlib.sha256(name.encode()).hexdigest(), name, name,
             "repo", _USER_NAME, _REPO_KEY, _REPO_KEY, org_id))
        cid = c.lastrowid
        # 种入引擎行（scope=repo, org_id=仓库所属组织）
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'repo',?,?,'online')",
            (cid, "t371-hermes", "t371-Hermes", _REPO_KEY, org_id))
        db.commit()
        return cid


def _get_cred_row(cid):
    """读取凭据行（scope/owner_id/org_id/repo_key）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scope, owner_id, org_id, repo_key FROM runtime_credentials WHERE id=?", (cid,))
        r = c.fetchone()
        return {"scope": r[0], "owner_id": r[1] or "", "org_id": r[2] or 0, "repo_key": r[3] or ""} if r else None


class TestChangeCredentialScopeSyncsOrgId:
    """Issue #371 改动 1：change_credential_scope 切换时同步 org_id + 修复 owner_id 清空 bug。"""

    def test_user_to_repo_syncs_org_id_to_repo_org(self):
        """user→repo 切换：org_id 对齐到仓库所属组织（managed_repos.org_id）。"""
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_user_scope_cred()
        # 切换前：org_id=公司组织
        assert _get_cred_row(cid)["org_id"] == _ORG_COMPANY
        # 切换为 repo scope
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user_in_company())
        row = _get_cred_row(cid)
        assert row["scope"] == "repo"
        assert row["repo_key"] == _REPO_KEY
        # Issue #371 核心：org_id 对齐到仓库所属组织
        assert row["org_id"] == _ORG_REPO_OWNER, f"org_id 应同步为仓库所属组织，实际={row['org_id']}"
        # owner_id 保留 repo_key（不再被无条件清空）
        assert row["owner_id"] == _REPO_KEY, f"repo scope owner_id 应为 repo_key，实际={row['owner_id']}"

    def test_repo_to_user_syncs_org_id_to_operator_org(self):
        """repo→user 切换：org_id 对齐到操作者所在组织（设备在操作组织下可见）。"""
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_repo_scope_cred()
        # 切换前：org_id=仓库所属组织
        assert _get_cred_row(cid)["org_id"] == _ORG_REPO_OWNER
        # 切换为 user scope（在社区博客团队组织下操作）
        change_credential_scope(cid, CredScopeBody(scope="user"), user=_user_in_company())
        row = _get_cred_row(cid)
        assert row["scope"] == "user"
        # org_id 对齐到操作者所在组织
        assert row["org_id"] == _ORG_COMPANY, f"user scope org_id 应对齐到操作组织({_ORG_COMPANY})，实际={row['org_id']}"
        # owner_id 清空（user scope 注册时按 owner_name 重算）
        assert row["owner_id"] == "", f"user scope owner_id 应清空，实际={row['owner_id']}"

    def test_repo_scope_owner_id_not_wiped(self):
        """Issue #371 改动 1B：repo scope 切换后 owner_id 不再被无条件清空（line 1288 bug 修复）。"""
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_user_scope_cred()
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user_in_company())
        row = _get_cred_row(cid)
        # 修复前：owner_id='' （line 1268 写入后被 line 1288 清空）
        # 修复后：owner_id=repo_key
        assert row["owner_id"] == _REPO_KEY, "repo scope owner_id 不应被清空"

    def test_scope_change_deletes_old_engine_rows(self):
        """切换 scope 后旧 scope 引擎行被删除（无孤儿/重复引擎行）。"""
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_user_scope_cred()
        # 切换前有 1 条 user scope 引擎行
        with db_lock:
            c = db.cursor()
            before = c.execute(
                "SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=?", (cid,)).fetchone()[0]
            assert before == 1
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user_in_company())
        # 切换后旧 user scope 引擎行应被删除（worker 心跳后按新 scope 重建）
        with db_lock:
            c = db.cursor()
            after = c.execute(
                "SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=?", (cid,)).fetchone()[0]
            assert after == 0, f"切换后旧引擎行应被删除，实际剩余 {after} 行"


class TestRepoScopeVisibleInNonPersonalOrg:
    """Issue #371 改动 2：非个人组织视图 repo scope 设备/引擎可见性豁免。"""

    def test_repo_scope_engine_visible_in_non_personal_org(self):
        """社区博客团队（非个人组织）视图：repo scope 引擎可见（切换后 org_id=仓库组织 ≠ 当前组织）。"""
        from src.api.routes.settings import get_agent_runtimes
        _create_repo_scope_cred()  # org_id=仓库所属组织（≠ _ORG_COMPANY）
        d = get_agent_runtimes(user=_user_in_company())
        engine_names = {rt["name"] for rt in d["runtimes"]}
        assert "t371-hermes" in engine_names, f"repo scope 引擎在非个人组织视图应可见: {engine_names}"

    def test_repo_scope_device_visible_in_non_personal_org(self):
        """社区博客团队视图：runtime-devices 列出 repo scope 设备。"""
        from src.api.routes.settings import get_runtime_devices
        _create_repo_scope_cred()
        d = get_runtime_devices(user=_user_in_company(), page=1, page_size=100)
        names = {dev["name"] for dev in d["devices"]}
        assert "t371-worker-repo" in names, f"repo scope 设备在非个人组织视图应可见: {names}"

    def test_repo_scope_visible_after_scope_change(self):
        """端到端：user→repo 切换后刷新 Workers 设备页引擎可见（Issue #371 核心验收场景）。"""
        from src.api.routes.settings import change_credential_scope, CredScopeBody, get_agent_runtimes, get_runtime_devices
        cid = _create_user_scope_cred()
        # 切换前：user scope 引擎在公司组织视图可见
        d_before = get_agent_runtimes(user=_user_in_company())
        assert "t371-opencode" in {rt["name"] for rt in d_before["runtimes"]}
        # 切换为 repo scope
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user_in_company())
        # 切换后删了旧行，需模拟 worker 心跳重建引擎行（按新 scope+owner_id+org_id）
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
                "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'repo',?,?,'online')",
                (cid, "t371-hermes", "t371-Hermes", _REPO_KEY, _ORG_REPO_OWNER))
            db.commit()
        # 刷新 Workers 设备页：repo scope 引擎应可见
        d_after = get_agent_runtimes(user=_user_in_company())
        assert "t371-hermes" in {rt["name"] for rt in d_after["runtimes"]}, \
            "切换为仓库级后 Workers 设备页引擎应可见"
        # runtime-devices 端点同样可见（设备名=凭据名，切换不改名）
        d_dev = get_runtime_devices(user=_user_in_company(), page=1, page_size=100)
        assert "t371-worker-user" in {dev["name"] for dev in d_dev["devices"]}, \
            "切换为仓库级后设备卡片应可见"
        # 确认设备 scope 已更新为 repo
        dev = next(x for x in d_dev["devices"] if x["name"] == "t371-worker-user")
        assert dev["scope"] == "repo", f"设备 scope 应为 repo，实际={dev['scope']}"


class TestRegression338PersonalOrgVisibility:
    """回归 #338：个人组织视图个人归属 worker 设备可见不回退。"""

    def test_own_user_worker_visible_in_personal_org(self):
        """个人组织视图：user scope 且 owner=当前用户的设备/引擎可见。"""
        from src.api.routes.settings import get_runtime_devices, get_agent_runtimes
        _create_user_scope_cred()
        d = get_runtime_devices(user=_user_in_personal(), page=1, page_size=100)
        assert "t371-worker-user" in {dev["name"] for dev in d["devices"]}, d
        r = get_agent_runtimes(user=_user_in_personal())
        assert "t371-opencode" in {rt["name"] for rt in r["runtimes"]}, r


class TestRegression277RepoIsolation:
    """回归 #277：仓库隔离不越权（非仓库成员看不到 repo scope 设备）。"""

    def test_repo_scope_isolated_by_repo_filter(self):
        """repo_filter 非空时，repo scope 仅允许当前仓库（#277 核心场景不回退）。"""
        from src.api.routes.settings import _make_visible_credential_fn
        # 用户可访问 repo-a，但凭据绑定 repo-b → 不可见
        user_repos = {"songspace/repo-a"}
        vis = _make_visible_credential_fn(_USER_NAME, user_repos, repo_filter="songspace/repo-a")
        cid = 999
        # repo-b 凭据（不在 user_repos）
        assert vis(cid, "repo", _USER_NAME, "gitcode:songspace/repo-b") is False
        # repo-a 凭据（在 user_repos）
        assert vis(cid, "repo", _USER_NAME, "gitcode:songspace/repo-a") is True

    def test_non_member_cannot_see_repo_scope_device(self):
        """非仓库成员在非个人组织视图看不到 repo scope 设备（_visible_credential 收敛，不越权）。"""
        from src.api.routes.settings import get_runtime_devices
        _create_repo_scope_cred()
        # 用户在另一家非个人组织（非仓库成员，user_repos 为空）
        user_other = {"id": 88, "username": "bob", "auth_type": "jwt", "org_id": _ORG_COMPANY_OTHER}
        d = get_runtime_devices(user=user_other, page=1, page_size=100)
        assert "t371-worker-repo" not in {dev["name"] for dev in d["devices"]}, \
            "非仓库成员不应看到 repo scope 设备"

    def test_other_org_strict_isolation_kept(self):
        """另一家非个人组织视图：org_id 不匹配的非 repo scope 引擎仍严格隔离。"""
        from src.api.routes.settings import get_agent_runtimes
        # user scope 引擎 org_id=公司组织 A，换到公司组织 B 视图应不可见
        _create_user_scope_cred()
        user_b = {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_COMPANY_OTHER}
        d = get_agent_runtimes(user=user_b)
        assert "t371-opencode" not in {rt["name"] for rt in d["runtimes"]}, \
            "非个人组织应严格隔离 user scope 引擎（org_id 不匹配）"
