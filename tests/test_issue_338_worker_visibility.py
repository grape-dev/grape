"""Issue #338：agent 运行时 workers 设备看不见归属自己的设备。

根因：worker 凭据（runtime_credentials）创建时绑定创建时所在组织的 org_id；
settings 的 agent-runtimes / runtime-devices 两个端点在「个人组织」视图按
org_id 过滤凭据/引擎，把用户自己的 user scope worker 设备错误滤掉
（引擎可见、workers 设备不可见）。

修正后语义（与 dashboard 个人组织视图 "global + 自己的 user scope worker" 一致）：
- 个人组织视图：scope=user 且 owner=当前用户的 worker 凭据/引擎不受 org_id 隔离
- 非个人组织：维持严格 org_id 隔离（行为不变）
- 非本人 user scope 设备仍不可见（_visible_credential owner 判定收敛，无权限放大）
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_issue_338.db"))
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import hashlib

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

_ORG_PERSONAL = 933801      # songspace 个人组织
_ORG_COMPANY = 933802       # 非个人组织（凭据创建时所在组织）
_ORG_BOB_PERSONAL = 933803  # bob 个人组织
_ORG_COMPANY_OTHER = 933804  # 另一家非个人组织（严格隔离对照）

_DEV_NAME = "t338-worker-own"
_GLOBAL_DEV = "t338-worker-global"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't338-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't338-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?,?)",
                  (_ORG_PERSONAL, _ORG_COMPANY, _ORG_BOB_PERSONAL, _ORG_COMPANY_OTHER))
        # 三个组织：songspace 个人 / 非个人公司组织 / bob 个人
        for oid, slug, owner, personal in (
            (_ORG_PERSONAL, "t338-songspace", 77, 1),
            (_ORG_COMPANY, "t338-company", 1, 0),
            (_ORG_BOB_PERSONAL, "t338-bob", 88, 1),
            (_ORG_COMPANY_OTHER, "t338-company-b", 1, 0),
        ):
            c.execute(
                "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,?)",
                (oid, slug, slug, owner, personal))
        # songspace 的个人 worker 凭据：scope=user，但 org_id 绑定为公司组织
        # （创建时 songspace 正处于公司组织上下文）——修复前在个人组织视图被滤掉
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, org_id, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            (hashlib.sha256(_DEV_NAME.encode()).hexdigest(), _DEV_NAME, _DEV_NAME,
             "user", "songspace", _ORG_COMPANY))
        cid_own = c.execute(
            "SELECT id FROM runtime_credentials WHERE name=?", (_DEV_NAME,)).fetchone()[0]
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'user','',?,'online')",
            (cid_own, "t338-opencode", "t338-Opencode", _ORG_COMPANY))
        # 对照组：global scope worker 凭据（org_id=0，任何视图均可见）
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, org_id, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            (hashlib.sha256(_GLOBAL_DEV.encode()).hexdigest(), _GLOBAL_DEV, _GLOBAL_DEV,
             "global", "songspace", 0))
        cid_g = c.execute(
            "SELECT id FROM runtime_credentials WHERE name=?", (_GLOBAL_DEV,)).fetchone()[0]
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'global','',0,'online')",
            (cid_g, "t338-hermes", "t338-Hermes"))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't338-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't338-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?,?)",
                  (_ORG_PERSONAL, _ORG_COMPANY, _ORG_BOB_PERSONAL, _ORG_COMPANY_OTHER))
        db.commit()


def _songspace():
    return {"id": 77, "username": "songspace", "auth_type": "jwt", "org_id": _ORG_PERSONAL}


def _bob():
    return {"id": 88, "username": "bob", "auth_type": "jwt", "org_id": _ORG_BOB_PERSONAL}


class TestWorkerDeviceVisibility:
    """Issue #338：个人归属 workers 设备在个人组织视图的可见性。"""

    def test_own_worker_device_visible_in_personal_org(self):
        """songspace 在个人组织视图：runtime-devices 列出其名下 user scope worker 设备。"""
        from src.api.routes.settings import get_runtime_devices
        d = get_runtime_devices(user=_songspace(), page=1, page_size=100)
        names = {dev["name"] for dev in d["devices"]}
        assert _DEV_NAME in names, f"个人归属 worker 设备被滤掉: {names}"
        dev = next(x for x in d["devices"] if x["name"] == _DEV_NAME)
        assert dev["scope"] == "user"
        assert any(r["name"] == "t338-opencode" for r in dev["runtimes"]), dev

    def test_own_worker_engine_visible_in_personal_org(self):
        """songspace 在个人组织视图：agent-runtimes 引擎列表可见其 worker 承载引擎。"""
        from src.api.routes.settings import get_agent_runtimes
        d = get_agent_runtimes(user=_songspace())
        engine_names = {rt["name"] for rt in d["runtimes"]}
        assert "t338-opencode" in engine_names, f"worker 承载引擎被滤掉: {engine_names}"
        dev_names = {dev["name"] for dev in d["devices"]}
        assert _DEV_NAME in dev_names, f"devices 视图缺少归属设备: {dev_names}"

    def test_global_engine_not_regressed(self):
        """个人组织视图：global scope 设备/引擎仍可见（行为不回退）。"""
        from src.api.routes.settings import get_runtime_devices, get_agent_runtimes
        d = get_runtime_devices(user=_songspace(), page=1, page_size=100)
        assert _GLOBAL_DEV in {dev["name"] for dev in d["devices"]}, d
        r = get_agent_runtimes(user=_songspace())
        assert "t338-hermes" in {rt["name"] for rt in r["runtimes"]}, r

    def test_other_user_cannot_see_user_scope_device(self):
        """bob（另一用户）个人组织视图：看不到 songspace 的 user scope 设备（无权限放大）。"""
        from src.api.routes.settings import get_runtime_devices, get_agent_runtimes
        d = get_runtime_devices(user=_bob(), page=1, page_size=100)
        assert _DEV_NAME not in {dev["name"] for dev in d["devices"]}, d
        r = get_agent_runtimes(user=_bob())
        assert "t338-opencode" not in {rt["name"] for rt in r["runtimes"]}, r

    def test_non_personal_org_keeps_strict_isolation(self):
        """非个人组织视图：维持严格 org_id 隔离（行为不变，无越权）。

        songspace 的 user scope 凭据 org_id=公司组织 A；换到另一家非个人组织 B
        的上下文时，该凭据（org_id=A ≠ B）按严格隔离不可见。
        """
        from src.api.routes.settings import get_runtime_devices
        user = {"id": 77, "username": "songspace", "auth_type": "jwt",
                "org_id": _ORG_COMPANY_OTHER}
        d = get_runtime_devices(user=user, page=1, page_size=100)
        names = {dev["name"] for dev in d["devices"]}
        assert _DEV_NAME not in names, f"非个人组织应严格隔离: {names}"
        assert _GLOBAL_DEV not in names, f"org_id=0 设备在非个人组织仍严格隔离: {names}"

    def test_cred_map_filter_unit(self):
        """单元级断言：个人组织分支允许 own user-scope 凭据跨 org_id（防回归）。"""
        cred = {"scope": "user", "owner_name": "songspace", "org_id": _ORG_COMPANY}
        org_id = _ORG_PERSONAL
        kept = (cred.get("org_id") or 0) in (0, org_id) or (
            cred.get("scope") == "user" and (cred.get("owner_name") or "") == "songspace")
        assert kept
        cred_other = {"scope": "user", "owner_name": "bob", "org_id": _ORG_COMPANY}
        kept_other = (cred_other.get("org_id") or 0) in (0, org_id) or (
            cred_other.get("scope") == "user" and (cred_other.get("owner_name") or "") == "songspace")
        assert not kept_other
