"""Issue #473 PR #668 审查修复：range=user 含本机引擎（cid=0）+ 未知 range 校验。

QA Required 项：_rt_in_range 的 is_local 分支以输出 owner（恒空）判定归属为死代码——
本机引擎（credential_id=0）的 get_agent_runtimes 输出 owner 取自 cred_map.get(0)（不存在，
落默认 owner_name=""），本机设备行 owner 亦为 ""，故 `(owner or "") == uname` 恒 False。
修复：is_local 条目改用引擎自身 DB 行 owner_id（agent_runtimes.owner_id）判定归属。

Nice-to-have 同步落地：未知 range 值 422（FastAPI Literal 校验），不再静默回落全量。

验证点：
1. range=user → 本机引擎（cid=0、owner_id=当前用户）可见（Engines + Workers 设备）
2. range=user → 他人本机引擎（cid=0、owner_id=他人）不可见
3. range=user → owner_id 为空的遗留本机引擎（无主）不误入个人范围
4. range 非法值（如 "typo"）→ 422，不静默回落全量
5. range=user/rep/all 合法值不受 Literal 校验影响（回归，与既有 10 例互补）
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
_ORG_COMPANY = 947102       # 社区博客团队（非个人组织）
_ORG_REPO_OWNER = 947104

_USER_ID = 87
_USER_NAME = "songspace"
_OTHER_NAME = "bob"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't473f-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't473f-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?)", (_ORG_PERSONAL, _ORG_COMPANY, _ORG_REPO_OWNER))
        c.execute("DELETE FROM grape_accounts WHERE id IN (?,?)", (_USER_ID, 88))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,1)",
            (_ORG_PERSONAL, "t473-songspace", "t473-songspace", _USER_ID))
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,0)",
            (_ORG_COMPANY, "t473-company", "t473-company", _USER_ID))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)", (_USER_ID, _USER_NAME))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)", (88, _OTHER_NAME))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't473f-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't473f-%'")
        c.execute("DELETE FROM organization WHERE id IN (?,?,?)", (_ORG_PERSONAL, _ORG_COMPANY, _ORG_REPO_OWNER))
        c.execute("DELETE FROM grape_accounts WHERE id IN (?,?)", (_USER_ID, 88))
        db.commit()


def _user():
    """当前登录用户（个人组织视图：本机引擎 org_id=0 可见，与生产本机引擎口径一致）。"""
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_PERSONAL}


def _mk_local_engine(name, owner_id):
    """插入本机直连引擎行（credential_id=0），owner_id 取引擎自身 DB 值。

    对齐 src.user.db.save_agent_runtimes 的本机行写入口径：
    INSERT ... (name, ..., scope, owner_id, host_name) VALUES (..., 'global', owner_id, host)。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
            "scope, owner_id, org_id, status, host_name) VALUES (0,?,?,1,1,'global',?,?, 'online', ?)",
            (name, name, owner_id, 0, "t473f-host"))
        db.commit()
        return c.lastrowid


class TestRangeUserLocalEngine:
    """QA Required：range=user 的「含本机引擎」实际生效（以 DB 行 owner_id 判定归属）。"""

    def test_own_local_engine_visible_in_user_range(self):
        """本人本机引擎（cid=0、owner_id=本人）→ range=user 可见。"""
        _mk_local_engine("t473f-opencode", _USER_NAME)
        from src.api.routes.settings import get_agent_runtimes, get_runtime_devices
        d = get_agent_runtimes(user=_user(), range="user")
        names = {rt["name"] for rt in d["runtimes"]}
        assert "t473f-opencode" in names, f"本人本机引擎应可见: {names}"
        d2 = get_runtime_devices(user=_user(), range="user", page=1, page_size=100)
        # 本机引擎聚合到本机设备（device_name=host_name 或「本机直连」）
        local_rts = [r["name"] for dev in d2["devices"] for r in dev.get("runtimes", [])]
        assert "t473f-opencode" in local_rts, f"Workers 本机设备应含本人本机引擎: {local_rts}"

    def test_other_user_local_engine_hidden_in_user_range(self):
        """他人本机引擎（cid=0、owner_id=bob）→ range=user 不可见。"""
        _mk_local_engine("t473f-aishell", _OTHER_NAME)
        from src.api.routes.settings import get_agent_runtimes, get_runtime_devices
        d = get_agent_runtimes(user=_user(), range="user")
        names = {rt["name"] for rt in d["runtimes"]}
        assert "t473f-aishell" not in names, f"他人本机引擎不应出现在个人范围: {names}"
        d2 = get_runtime_devices(user=_user(), range="user", page=1, page_size=100)
        local_rts = [r["name"] for dev in d2["devices"] for r in dev.get("runtimes", [])]
        assert "t473f-aishell" not in local_rts

    def test_unowned_local_engine_not_in_user_range(self):
        """无主遗留本机引擎（owner_id 空）→ 不误入个人范围（保持既有全量可见口径）。"""
        rid = _mk_local_engine("t473f-hermes", "")
        from src.api.routes.settings import get_agent_runtimes
        d = get_agent_runtimes(user=_user(), range="user")
        names = {rt["name"] for rt in d["runtimes"]}
        assert "t473f-hermes" not in names, "无主本机引擎不应误入个人范围"
        # 回归：range=all 全量口径下仍可见（不因修复收窄全量视图）
        d2 = get_agent_runtimes(user=_user(), range="all")
        assert "t473f-hermes" in {rt["name"] for rt in d2["runtimes"]}, "全量口径下无主本机引擎应仍可见"


class TestRangeInvalidValue:
    """QA Nice-to-have：未知 range 值不静默回落全量（FastAPI Literal 校验 422）。"""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user, get_current_user_or_api_key
        app.dependency_overrides[get_current_user] = lambda: _user()
        app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
        yield TestClient(app)
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("bad", ["typo", "ALL", "users"])
    def test_invalid_range_returns_422(self, client, bad):
        resp = client.get("/api/settings/agent-runtimes", params={"range": bad})
        assert resp.status_code == 422, f"非法 range 应 422 而非静默回落全量: {resp.status_code}"
        resp2 = client.get("/api/settings/runtime-devices", params={"range": bad})
        assert resp2.status_code == 422, f"runtime-devices 非法 range 应 422: {resp2.status_code}"

    def test_valid_ranges_not_rejected(self, client):
        """合法值（user/repo/all/空）不受 Literal 校验影响（回归）。"""
        _mk_local_engine("t473f-opencode", _USER_NAME)
        for good in ("user", "repo", "all", ""):
            resp = client.get("/api/settings/agent-runtimes", params={"range": good})
            assert resp.status_code == 200, f"range={good!r} 合法值不应被拒: {resp.status_code}"
        resp2 = client.get("/api/settings/runtime-devices", params={"range": "user"})
        assert resp2.status_code == 200
