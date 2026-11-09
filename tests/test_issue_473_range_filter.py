"""Issue #473：设备与引擎页 runtime 列表范围筛选（全部/仓库/个人）。

验证点：
1. range=user → 仅当前登录用户名下 user scope 引擎/设备（不含 repo/global/他人 user scope）
2. range=repo → 仅 repo scope 引擎/设备（配合 #277 repo 参数时仅当前仓库）
3. range 空 / all → 恢复现有全量口径（全局 + 个人 + 我所在仓库的仓库级）
4. 范围与搜索(q)/scope 标签/分页组合正确，total 为组合筛选后总数
5. 组合筛选空结果返回空列表（不回退 #436 空结果修复）

数据种子（对齐 tests/test_issue_371_scope_change_visibility.py 风格）：
- user scope 凭据（owner=songspace）+ 引擎 t473-opencode
- repo scope 凭据（repo_key=gitcode:songspace/blog-team）+ 引擎 t473-hermes
- global scope 凭据 + 引擎 t473-codex
- 他人 user scope 凭据（owner=bob）+ 引擎 t473-aishell
"""
import os
import sys
import hashlib

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_issue_473.db"))
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
    os.environ.pop(_k, None)

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

_ORG_PERSONAL = 947101
_ORG_COMPANY = 947102       # 社区博客团队（非个人组织，Issue #371 场景：repo scope 豁免）
_ORG_REPO_OWNER = 947104

_USER_ID = 87
_USER_NAME = "songspace"
_OTHER_NAME = "bob"
_REPO_FULL = "songspace/blog-team"
_REPO_KEY = "gitcode:songspace/blog-team"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't473-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't473-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?)", (_ORG_PERSONAL, _ORG_COMPANY, _ORG_REPO_OWNER))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id IN (?,?)", (_USER_ID, 88))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,1)",
            (_ORG_PERSONAL, "t473-songspace", "t473-songspace", _USER_ID))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,0)",
            (_ORG_COMPANY, "t473-company", "t473-company", _USER_ID))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) "
                  "VALUES (?,?,?,?,?)", (_REPO_KEY, _USER_NAME, "blog-team", _REPO_FULL, _ORG_REPO_OWNER))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)", (_USER_ID, _USER_NAME))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)", (88, _OTHER_NAME))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't473-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't473-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?)", (_ORG_PERSONAL, _ORG_COMPANY, _ORG_REPO_OWNER))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id IN (?,?)", (_USER_ID, 88))
        db.commit()


def _user():
    """当前登录用户（社区博客团队非个人组织视图：global + 个人 + 所在仓库可见，Issue #371 口径）。"""
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_COMPANY}


def _create_cred(name, scope, owner_name, repo_key="", org_id=0):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, owner_id, repo_key, org_id, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'active')",
            (hashlib.sha256(name.encode()).hexdigest(), name, name, scope, owner_name, "", repo_key, org_id))
        cid = c.lastrowid
        engine = {"t473-worker-user": "t473-opencode", "t473-worker-repo": "t473-hermes",
                  "t473-worker-global": "t473-codex", "t473-worker-bob": "t473-aishell"}[name]
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'user','',?,'online')",
            (cid, engine, engine, org_id))
        db.commit()
        return cid, engine


@pytest.fixture()
def seeded_all():
    """种入四种范围凭据，返回 (cid, engine) 列表。"""
    return [
        _create_cred("t473-worker-user", "user", _USER_NAME, org_id=_ORG_COMPANY),
        _create_cred("t473-worker-repo", "repo", _USER_NAME, repo_key=_REPO_KEY, org_id=_ORG_REPO_OWNER),
        _create_cred("t473-worker-global", "global", "admin", org_id=_ORG_COMPANY),
        _create_cred("t473-worker-bob", "user", _OTHER_NAME, org_id=_ORG_COMPANY),
    ]


class TestRangeUser:
    """range=user：仅当前登录用户名下 user scope 引擎/设备。"""

    def test_engines_only_own_user_scope(self, seeded_all):
        from src.api.routes.settings import get_agent_runtimes
        d = get_agent_runtimes(user=_user(), range="user")
        names = {rt["name"] for rt in d["runtimes"]}
        assert "t473-opencode" in names
        assert "t473-hermes" not in names, "range=user 不应含 repo scope 引擎"
        assert "t473-codex" not in names, "range=user 不应含 global 引擎"
        assert "t473-aishell" not in names, "range=user 不应含他人 user scope 引擎"

    def test_devices_only_own_user_scope(self, seeded_all):
        from src.api.routes.settings import get_runtime_devices
        d = get_runtime_devices(user=_user(), range="user", page=1, page_size=100)
        names = {dev["name"] for dev in d["devices"]}
        assert "t473-worker-user" in names
        assert "t473-worker-repo" not in names
        assert "t473-worker-global" not in names
        assert "t473-worker-bob" not in names


class TestRangeRepo:
    """range=repo：仅 repo scope 引擎/设备（复用 #277 口径）。"""

    def test_engines_only_repo_scope(self, seeded_all):
        from src.api.routes.settings import get_agent_runtimes
        d = get_agent_runtimes(user=_user(), range="repo")
        names = {rt["name"] for rt in d["runtimes"]}
        assert names == {"t473-hermes"}, f"range=repo 应仅 repo scope 引擎: {names}"

    def test_devices_only_repo_scope(self, seeded_all):
        from src.api.routes.settings import get_runtime_devices
        d = get_runtime_devices(user=_user(), range="repo", page=1, page_size=100)
        names = {dev["name"] for dev in d["devices"]}
        assert names == {"t473-worker-repo"}, f"range=repo 应仅 repo scope 设备: {names}"

    def test_repo_param_narrows_to_current_repo(self, seeded_all):
        """range=repo + repo 参数组合：仅当前仓库（#277 口径不回退）。"""
        from src.api.routes.settings import get_agent_runtimes, get_runtime_devices
        # 种入另一个仓库的 repo scope 凭据
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, owner_id, repo_key, org_id, status) "
                "VALUES (?,?,?,?,?,?,?,?, 'active')",
                (hashlib.sha256(b"t473-worker-repo-b").hexdigest(), "t473-worker-repo-b", "t473-worker-repo-b",
                 "repo", _USER_NAME, "", "gitcode:songspace/other-repo", _ORG_REPO_OWNER))
            _cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
                "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'repo',?,?,'online')",
                (_cid, "t473-codex-b", "t473-codex-b", "gitcode:songspace/other-repo", _ORG_REPO_OWNER))
            db.commit()
        d = get_agent_runtimes(user=_user(), range="repo", repo=_REPO_FULL)
        names = {rt["name"] for rt in d["runtimes"]}
        assert "t473-hermes" in names
        assert "t473-codex-b" not in names, "他仓 repo scope 引擎不应出现（#277）"
        d2 = get_runtime_devices(user=_user(), range="repo", repo=_REPO_FULL, page=1, page_size=100)
        dev_names = {dev["name"] for dev in d2["devices"]}
        assert "t473-worker-repo" in dev_names
        assert "t473-worker-repo-b" not in dev_names


class TestRangeDefault:
    """range 空 / all：恢复现有全量口径。"""

    def test_no_range_returns_all_visible(self, seeded_all):
        from src.api.routes.settings import get_agent_runtimes
        d = get_agent_runtimes(user=_user())
        names = {rt["name"] for rt in d["runtimes"]}
        assert {"t473-opencode", "t473-hermes", "t473-codex"} <= names

    def test_range_all_returns_all_visible(self, seeded_all):
        from src.api.routes.settings import get_agent_runtimes, get_runtime_devices
        d = get_agent_runtimes(user=_user(), range="all")
        names = {rt["name"] for rt in d["runtimes"]}
        assert {"t473-opencode", "t473-hermes", "t473-codex"} <= names
        d2 = get_runtime_devices(user=_user(), range="all", page=1, page_size=100)
        assert {"t473-worker-user", "t473-worker-repo", "t473-worker-global"} <= {x["name"] for x in d2["devices"]}


class TestRangeCombined:
    """范围与搜索/scope 标签/分页组合 + #436 空结果不回退。"""

    def test_range_repo_with_search(self, seeded_all):
        """range=repo + q 搜索：命中当前仓 repo 引擎，total 正确。"""
        from src.api.routes.settings import get_runtime_devices
        d = get_runtime_devices(user=_user(), range="repo", q="hermes", page=1, page_size=10)
        names = {dev["name"] for dev in d["devices"]}
        assert names == {"t473-worker-repo"}
        assert d["total"] == 1

    def test_range_user_with_search_no_result(self, seeded_all):
        """range=user + q 搜索无命中 → 空列表 + total=0（#436 不回退，不误报）。"""
        from src.api.routes.settings import get_runtime_devices, get_agent_runtimes
        d = get_runtime_devices(user=_user(), range="user", q="not-exist-keyword", page=1, page_size=10)
        assert d["devices"] == []
        assert d["total"] == 0
        r = get_agent_runtimes(user=_user(), range="user")
        # 引擎端点 q 过滤在前端，此处验证范围后列表非空且口径正确
        assert "t473-opencode" in {rt["name"] for rt in r["runtimes"]}

    def test_range_repo_with_scope_filter_and_pagination(self, seeded_all):
        """range=repo + scope=repo + 分页：total 为组合筛选后总数。"""
        from src.api.routes.settings import get_runtime_devices
        d = get_runtime_devices(user=_user(), range="repo", scope="repo", page=1, page_size=1)
        assert d["total"] == 1
        assert len(d["devices"]) == 1
        assert d["devices"][0]["name"] == "t473-worker-repo"
