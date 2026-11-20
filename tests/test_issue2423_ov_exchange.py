"""Issue #2423 M1/M2 连接点：exec 端点 tk_ 兑换扩展（凭据格子优先解析）。

Seam（公开接口）：POST /api/plugins/{name}/exec（tk_ 鉴权）。
- 凭据解析序：凭据格子（user_id×vault）→ user_credentials(plugin:{name})（存量自配兼容）；
- 吊销后 exec 400（不复活）——含存量自配回退路径（吊销台账阻断，QA (a)3）；
- 格子托管分派按 manifest 凭据标记/注册表，不在通用端点硬编码凭据键特例（Standards #5）；
- 两用户格子隔离（各自 X-API-Key）；OV key 只在服务端进程内注入（请求头断言），响应无明文。
"""
import json
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.main import app
from src.kernel.auth_sdk import auth_token
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.auth import grape_account_db
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

PLUGIN = "test-ov-plugin"
MARKER_PLUGIN = "test-marker-plugin"
OV_BASE = "http://ov.test"
REPO_KEY = "gitcode:testowner/myrepo"
ORG_ID = 0


def _manifest(name=PLUGIN):
    return {
        "name": name,
        "version": "0.0.1",
        "display_name": "测试 OV 插件",
        "schema_version": "1.0",
        "adapters": [{
            "type": "http", "transport": "http", "name": "rest",
            "base_url": OV_BASE,
            "auth": {"credential": "ov_api_key", "inject": "header:X-API-Key"},
            "compat": {"engines": ["opencode", "codearts", "codex", "hermes", "dsh", "hwcloud"]},
        }],
        "credentials": [{
            "name": "ov_api_key", "key": "ov_api_key", "type": "api_key",
            "required": True, "inject": "header:X-API-Key",
            "scope": ["coding", "qa", "sre"], "expiry": "none",
        }],
        "commands": [
            {"name": "health", "level": "read",
             "adapters": {"rest": {"method": "GET", "path": "/health"}},
             "params": {}, "timeout": 10},
        ],
    }


def _marker_manifest():
    """标记分派 manifest：凭据键非 ov_api_key，manifest 显式声明 managed=org_cell。"""
    m = _manifest(MARKER_PLUGIN)
    m["adapters"][0]["auth"] = {"credential": "team_ov_key", "inject": "header:X-API-Key"}
    m["credentials"] = [{
        "name": "team_ov_key", "key": "team_ov_key", "type": "api_key",
        "managed": "org_cell",
        "required": True, "inject": "header:X-API-Key",
    }]
    return m


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


@pytest.fixture()
def ov_mock(monkeypatch):
    """假 OV：记录调用头（断言凭据只在服务端拼装）。"""
    calls = []

    def fake_request(method, url, **kw):
        calls.append({"method": method, "url": url, "headers": kw.get("headers") or {}})
        return _FakeResp(200, {"status": "ok"})

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "request", fake_request)
    monkeypatch.setattr(requests_mod, "get", lambda url, **kw: fake_request("GET", url, **kw))
    monkeypatch.setattr(requests_mod, "post", lambda url, **kw: fake_request("POST", url, **kw))
    return calls


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", OV_BASE)
    monkeypatch.setenv("PLUGIN_SNAPSHOT_PLUGINS", PLUGIN)
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_plugin WHERE name=?", (PLUGIN,))
        c.execute("DELETE FROM grape_t_plugin WHERE name=?", (MARKER_PLUGIN,))
        c.execute("DELETE FROM grape_t_plugin_call_logs WHERE plugin=?", (PLUGIN,))
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM user_credentials WHERE provider=?", (f"plugin:{PLUGIN}",))
        c.execute("DELETE FROM user_credentials WHERE provider=?", (f"plugin:{MARKER_PLUGIN}",))
        c.execute("DELETE FROM grape_t_ov_credential_cells WHERE org_id=?", (ORG_ID,))
        c.execute("DELETE FROM grape_t_ov_credential_cell_revocations WHERE org_id=?", (ORG_ID,))
        for uid in _user_ids():
            c.execute("DELETE FROM grape_t_ov_credential_cells WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_t_ov_credential_cell_revocations WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_t_plugin_credential_vault WHERE owner_id=?", (str(uid),))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        for rk in _ORG_TEST_REPOS:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        _ORG_TEST_REPOS.clear()
        db.commit()


_owners = []


def _mk_user(prefix: str) -> dict:
    user = grape_account_db.create_grape_account(f"{prefix}_{uuid.uuid4().hex[:8]}",
                                                 email=None, password="pw")
    _owners.append(user["id"])
    return user


def _user_ids():
    return _owners


def _market_upsert(manifest=None):
    with db_lock:
        m = manifest or _manifest()
        db.cursor().execute("DELETE FROM grape_t_plugin WHERE name=?", (m["name"],))
        db.cursor().execute(
            "INSERT INTO grape_t_plugin (id, name, version, display_name, description, manifest, "
            "manifest_hash, category_id, author, like_count, object_key, content_hash, "
            "namespace_owner, scope, owner_id, created_by, status, certification, org_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, m["name"], "0.0.1", m["name"], "", json.dumps(m, ensure_ascii=False),
             uuid.uuid4().hex, 0, "tester", 0, "", "", "", "user", 1, 1, "active", "none", 0))
        db.commit()


def _tk(user):
    return auth_token.issue_session_token(
        session_key=f"ovx-{uuid.uuid4().hex[:8]}", repo_key=REPO_KEY,
        agent_type="coding", user_id=user["id"])


def _tk_repo(user, repo_key):
    """tk_ 绑定指定 repo_key（org 选格用：任务 repo 决定取格 org）。"""
    return auth_token.issue_session_token(
        session_key=f"ovx-{uuid.uuid4().hex[:8]}", repo_key=repo_key,
        agent_type="coding", user_id=user["id"])


_ORG_TEST_REPOS = []


def _add_managed_repo(repo_key, org_id):
    """托管仓库行（org 选格路由数据源：managed_repos.org_id 权威口径）。"""
    owner, name = repo_key.split(":", 1)[1].rsplit("/", 1)
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) "
            "VALUES (?,?,?,?,?)", (repo_key, owner, name, f"{owner}/{name}", org_id))
        db.commit()
    _ORG_TEST_REPOS.append(repo_key)


def _exec(tk, command="health", plugin=PLUGIN):
    return client.post(f"/api/plugins/{plugin}/exec", json={"command": command},
                       headers={"Authorization": f"Bearer {tk}"})


# ---------- 兑换路径：格子优先 / 存量兼容 / 隔离 / 吊销不复活 ----------

def test_cell_only_credential_exchange(ov_mock):
    """仅格子配置（无 user_credentials）→ exec 兑换成功，OV 请求头带格子密钥。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    user = _mk_user("ovxc")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-1")
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["headers"].get("X-API-Key") == "cell-key-1"
    assert "cell-key-1" not in r.text


def test_cell_takes_priority_over_user_credentials(ov_mock):
    """格子优先于存量 user_credentials（管理端配置权威）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    user = _mk_user("ovxp")
    _market_upsert()
    user_db.set_credential(user["id"], f"plugin:{PLUGIN}", {"ov_api_key": "legacy-key"})
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-2")
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock[0]["headers"].get("X-API-Key") == "cell-key-2"


def test_user_credentials_fallback_still_works(ov_mock):
    """无格子时存量 user_credentials 兼容（自配路径不破坏）。"""
    user = _mk_user("ovxf")
    _market_upsert()
    user_db.set_credential(user["id"], f"plugin:{PLUGIN}", {"ov_api_key": "legacy-key-3"})
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock[0]["headers"].get("X-API-Key") == "legacy-key-3"


def test_two_users_identity_isolation(ov_mock):
    """两用户各自格子 → 各自任务兑换到的 OV 身份不同（落账身份隔离）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    u1 = _mk_user("ovxi1")
    u2 = _mk_user("ovxi2")
    _market_upsert()
    put_cell(u1["id"], u1["id"], ORG_ID, "ov-key-u1")
    put_cell(u2["id"], u2["id"], ORG_ID, "ov-key-u2")
    _exec(_tk(u1))
    assert ov_mock[0]["headers"].get("X-API-Key") == "ov-key-u1"
    _exec(_tk(u2))
    assert ov_mock[1]["headers"].get("X-API-Key") == "ov-key-u2"


def test_revoked_credential_no_resurrect(ov_mock):
    """吊销后 exec 400（不复活：无缓存副本，每次实时解析）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell, revoke_cell
    user = _mk_user("ovxr")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "ov-key-r")
    tk = _tk(user)
    r = _exec(tk)
    assert r.status_code == 200, r.text
    assert revoke_cell(user["id"], ORG_ID) is True
    r2 = _exec(tk)
    assert r2.status_code == 400, r2.text
    assert "PLUGIN_CREDENTIAL_MISSING" in r2.text
    assert len(ov_mock) == 1      # 吊销后未再发任何 OV 请求


def test_revoked_cell_blocks_legacy_fallback(ov_mock):
    """吊销闭环（QA 200711627 (a)3）：格子吊销后，存量自配 user_credentials 不得兜底复活。

    管理员吊销语义 = 收回该用户的 OV 访问——用户若另有存量自配凭据，
    须经吊销台账阻断回退（重新配格子方可恢复）。
    """
    from src.domains.mgmt.auth.credential_cell import put_cell, revoke_cell
    user = _mk_user("ovxrl")
    _market_upsert()
    user_db.set_credential(user["id"], f"plugin:{PLUGIN}", {"ov_api_key": "legacy-key-r"})
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-r")
    tk = _tk(user)
    r = _exec(tk)
    assert r.status_code == 200, r.text
    assert ov_mock[0]["headers"].get("X-API-Key") == "cell-key-r"   # 格子优先
    assert revoke_cell(user["id"], ORG_ID) is True
    r2 = _exec(tk)
    assert r2.status_code == 400, r2.text                           # 回退被吊销台账阻断
    assert "PLUGIN_CREDENTIAL_MISSING" in r2.text
    assert "吊销" in r2.text
    assert len(ov_mock) == 1      # 吊销后未再发任何 OV 请求
    # 重新配格子 → 恢复（吊销阻断的是回退，不是永久拉黑）
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-r2")
    r3 = _exec(tk)
    assert r3.status_code == 200, r3.text
    assert ov_mock[1]["headers"].get("X-API-Key") == "cell-key-r2"


def test_manifest_marker_dispatches_cell_credential(ov_mock):
    """格子托管分派按 manifest 标记（Standards #5）：cred_spec 声明 managed=org_cell
    → 走格子解析，通用 exec 端点不硬编码 ov_api_key 键特例。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    user = _mk_user("ovxm")
    _market_upsert(_marker_manifest())
    put_cell(user["id"], user["id"], ORG_ID, "marker-cell-key")
    r = _exec(_tk(user), plugin=MARKER_PLUGIN)
    assert r.status_code == 200, r.text
    assert ov_mock[0]["headers"].get("X-API-Key") == "marker-cell-key"
    assert "marker-cell-key" not in r.text


# ---------- #2544：exec 兑换按格子 base_url 路由（多 OV 实例部署） ----------
# base_url 取值序：格子 base_url（resolve_cell 命中且非空）> manifest adapter 默认；
# 白名单校验后置到最终 base_url 确定之后（deny-by-default 不变）。

OV2_BASE = "http://ov-2.test"
OV_EVIL_BASE = "http://ov-evil.test"


def test_cell_base_url_routes_exec_to_cell_instance(monkeypatch, ov_mock):
    """验收1：格子指向非 manifest 实例 → exec 兑换请求打到格子 base_url。

    多 OV 实例部署：管理员经 M2b 给用户配的 key 属新实例（格子里），
    exec 外呼目标必须是格子 base_url，凭据才不错配实例（否则必然 401）。
    """
    from src.domains.mgmt.auth.credential_cell import put_cell
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", f"{OV_BASE},{OV2_BASE}")
    user = _mk_user("ovx2544a")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-2544", base_url=OV2_BASE)
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["url"] == OV2_BASE + "/health"
    assert not any(c["url"].startswith(OV_BASE + "/") for c in ov_mock)
    assert ov_mock[0]["headers"].get("X-API-Key") == "cell-key-2544"


def test_no_cell_keeps_manifest_base_url(ov_mock):
    """验收2：无格子（存量自配路径）→ 保持 manifest 默认 base_url（现状兼容）。"""
    user = _mk_user("ovx2544b")
    _market_upsert()
    user_db.set_credential(user["id"], f"plugin:{PLUGIN}", {"ov_api_key": "legacy-2544"})
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["url"] == OV_BASE + "/health"


def test_cell_empty_base_url_falls_back_to_manifest(ov_mock):
    """验收2（兼容决策）：格子 base_url 为空 → 回退 manifest 默认（secret 仍有效）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    user = _mk_user("ovx2544c")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-2544-empty", base_url="")
    r = _exec(_tk(user))
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["url"] == OV_BASE + "/health"
    assert ov_mock[0]["headers"].get("X-API-Key") == "cell-key-2544-empty"


def test_cell_base_url_not_whitelisted_403(monkeypatch, ov_mock):
    """验收3：白名单对最终 base_url 生效——格子 base_url 未入名单 → 403 且零外呼。

    manifest 实例在白名单内但格子实例不在 → 必须按真实外呼目标（格子 base_url）拒绝，
    不得因 manifest URL 合法而放行打到未审批实例。
    403 消息须注明被拒 base_url 来源（QA 复审观察项：格子 vs manifest，排障不误导）。
    """
    from src.domains.mgmt.auth.credential_cell import put_cell
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", OV_BASE)
    user = _mk_user("ovx2544d")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-2544-evil", base_url=OV_EVIL_BASE)
    r = _exec(_tk(user))
    assert r.status_code == 403, r.text
    assert "PLUGIN_BASE_URL_BLOCKED" in r.text
    assert "凭据格子" in r.text          # 被拒来源注明：格子 base_url，非 manifest
    assert not ov_mock      # 白名单拒绝先于任何外呼


def test_cell_base_url_whitelist_unset_still_503(monkeypatch, ov_mock):
    """deny-by-default 不变：白名单未配置 → 全局拒绝 503（格子路径同样成立）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    monkeypatch.delenv("PLUGIN_ALLOWED_BASE_URLS", raising=False)
    user = _mk_user("ovx2544e")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG_ID, "cell-key-2544-unset", base_url=OV2_BASE)
    r = _exec(_tk(user))
    assert r.status_code == 503, r.text
    assert "PLUGIN_WHITELIST_UNSET" in r.text
    assert not ov_mock


# ---------- #2544 QA 复审补充：exec 按 org 选格 + 错误优先级固化 ----------
# 评论 200766500：一人多 org 各配格子时，exec 兑换须按任务 repo_key → org 选格，
# 防止跨 org 身份错用（当前 org 的任务误用另一 org 的 OV 身份落账）。

ORG2_ID, ORG4_ID, ORG5_ID = 101, 104, 105
ORG2_REPO = "gitcode:org-two/repo-a"
ORG5_REPO = "gitcode:org-five/repo-b"
OV3_BASE = "http://ov-3.test"


def test_exec_resolves_cell_by_task_org(monkeypatch, ov_mock):
    """补充缺陷（评论 200766500）：任务 repo 有 org 映射 → 按 org 取格。

    用户在 org2、org4 各配一格（org4 后配=最近更新）；org2 仓库的 exec 任务
    必须用 org2 格的 key 与 base_url，不得错用 org4 身份（跨 org 落账）。
    """
    from src.domains.mgmt.auth.credential_cell import put_cell
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", f"{OV_BASE},{OV3_BASE},{OV_EVIL_BASE}")
    user = _mk_user("ovxorg")
    _market_upsert()
    _add_managed_repo(ORG2_REPO, ORG2_ID)
    put_cell(user["id"], user["id"], ORG2_ID, "org2-key", base_url=OV3_BASE)
    put_cell(user["id"], user["id"], ORG4_ID, "org4-key", base_url=OV_EVIL_BASE)
    r = _exec(_tk_repo(user, ORG2_REPO))
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["headers"].get("X-API-Key") == "org2-key"
    assert ov_mock[0]["url"] == OV3_BASE + "/health"
    assert not any(c["url"].startswith(OV_EVIL_BASE + "/") for c in ov_mock)
    assert "org4-key" not in r.text


def test_exec_org_without_cell_no_cross_org_borrow(monkeypatch, ov_mock):
    """任务 org 未配格 → 不得借用其他 org 的格子（跨 org 身份错用），缺凭据 400。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", f"{OV_BASE},{OV_EVIL_BASE}")
    user = _mk_user("ovxnb")
    _market_upsert()
    _add_managed_repo(ORG5_REPO, ORG5_ID)
    put_cell(user["id"], user["id"], ORG4_ID, "org4-key-nb", base_url=OV_EVIL_BASE)
    r = _exec(_tk_repo(user, ORG5_REPO))
    assert r.status_code == 400, r.text
    assert "PLUGIN_CREDENTIAL_MISSING" in r.text
    assert not ov_mock      # 未外呼（未错用 org4 身份）


def test_exec_no_org_mapping_keeps_latest_cell(ov_mock):
    """无 repo→org 映射（个人仓等）→ 回退现行为：最近更新格（org4 后配者胜出）。"""
    from src.domains.mgmt.auth.credential_cell import put_cell
    user = _mk_user("ovxnm")
    _market_upsert()
    put_cell(user["id"], user["id"], ORG2_ID, "org2-key-nm")
    put_cell(user["id"], user["id"], ORG4_ID, "org4-key-nm")
    r = _exec(_tk(user))    # REPO_KEY 无 managed_repos 行（无 org 映射）
    assert r.status_code == 200, r.text
    assert ov_mock and ov_mock[0]["headers"].get("X-API-Key") == "org4-key-nm"


def test_missing_credential_precedes_whitelist_errors(monkeypatch, ov_mock):
    """错误优先级固化（QA 复审低危项）：凭据缺失（400）先于白名单 503/403 返回。

    凭据解析先行于 final_base_url 确定是 #2544 取值序的必然结构，
    本测试冻结该错误序（均为拒绝，deny 语义不变）。
    """
    user = _mk_user("ovxpo")
    _market_upsert()
    monkeypatch.delenv("PLUGIN_ALLOWED_BASE_URLS", raising=False)
    r1 = _exec(_tk(user))   # 无凭据 + 白名单未配置 → 400（而非 503）
    assert r1.status_code == 400, r1.text
    assert "PLUGIN_CREDENTIAL_MISSING" in r1.text
    monkeypatch.setenv("PLUGIN_ALLOWED_BASE_URLS", "http://not-in-list.test")
    r2 = _exec(_tk(user))   # 无凭据 + manifest base 不在白名单 → 400（而非 403）
    assert r2.status_code == 400, r2.text
    assert "PLUGIN_CREDENTIAL_MISSING" in r2.text
    assert not ov_mock
