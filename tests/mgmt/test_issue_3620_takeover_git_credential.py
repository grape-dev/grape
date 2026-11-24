"""Issue #3620：tko_ 接管凭据可通过 git-credential 取平台凭据（跨环境接管闭环）。

覆盖：
- tko_ 正向：repo_key 与请求仓库一致 + 接管会话 active → 200 返回平台凭据；
- tko_ 越权：跨仓 / 未绑定 repo_key → 403（fail-closed）；
- tko_ 接管会话已结束 → 403；
- 三凭据边界：tk_ 放行、tko_ 放行、grape_（API Key）/ JWT 仍 403
  （判定由能力位 repo_session 驱动，见 src/api/auth_caps.py）。
"""
import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.domains.mgmt.routes import repos as repos_mod

OWNER = "acme"
REPO = "web"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"
BOT_TOKEN = "bot_tok_3620_SECRET"


def _takeover_user(repo_key=REPO_KEY, session_key="takeover:t_3620"):
    return {"id": 42, "username": "taker", "auth_type": "takeover",
            "token": "tko_x", "token_type": "takeover", "kind": "takeover",
            "repo_key": repo_key, "agent_type": "coding",
            "session_key": session_key}


def _session_user():
    return {"id": 42, "username": "", "auth_type": "session_token",
            "token": "tk_x", "token_type": "session_token",
            "repo_key": REPO_KEY, "agent_type": "coding", "session_key": "t_3620"}


def _api_key_user():
    return {"id": 42, "username": "taker", "auth_type": "api_key",
            "token": "grape_x", "token_type": "grape_", "api_token_id": 7}


@pytest.fixture
def env(monkeypatch):
    def _set(user):
        app.dependency_overrides[get_current_user_or_api_key] = lambda: user

    _set(_takeover_user())
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token",
                        lambda **_kw: BOT_TOKEN)
    monkeypatch.setattr(repos_mod, "_takeover_session_status", lambda _tid: "active")
    yield TestClient(app), _set
    app.dependency_overrides.clear()


def _get(client):
    return client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")


def test_takeover_token_gets_credential(env):
    """tko_ 正向：同仓 + 会话 active → 返回平台凭据。"""
    client, _ = env
    r = _get(client)
    assert r.status_code == 200
    body = r.json()
    assert body["password"] == BOT_TOKEN
    assert body["clone_url"] == f"https://gitcode.com/{OWNER}/{REPO}.git"


def test_takeover_token_cross_repo_forbidden(env):
    """tko_ 越权：repo_key 指向别的仓库 → 403。"""
    client, set_user = env
    set_user(_takeover_user(repo_key="gitcode:acme/other"))
    r = _get(client)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == 403
    assert "仅限其任务仓库" in detail["message"]


def test_takeover_token_without_repo_key_forbidden(env):
    """tko_ 未绑定 repo_key → fail-closed 403（不得凭 path 推断放行）。"""
    client, set_user = env
    set_user(_takeover_user(repo_key=""))
    r = _get(client)
    assert r.status_code == 403
    assert "未绑定仓库" in r.json()["detail"]["message"]


def test_takeover_session_finished_forbidden(env, monkeypatch):
    """接管会话已结束（finish/abort 吊销）→ 403 二次校验拦截。"""
    client, _ = env
    monkeypatch.setattr(repos_mod, "_takeover_session_status", lambda _tid: "finished")
    r = _get(client)
    assert r.status_code == 403
    assert "接管会话已结束" in r.json()["detail"]["message"]


def test_session_token_still_allowed(env):
    """tk_ worker 会话能力边界不变（repo_session=True）。"""
    client, set_user = env
    set_user(_session_user())
    assert _get(client).status_code == 200


@pytest.mark.parametrize("auth_type", ["api_key", "jwt", "internal"])
def test_non_repo_session_credentials_still_forbidden(env, auth_type):
    """grape_ / JWT / internal 无 repo_session 能力 → 仍 403。"""
    client, set_user = env
    user = _api_key_user() if auth_type == "api_key" else {
        "id": 42, "username": "taker", "auth_type": auth_type, "token": "x"}
    set_user(user)
    r = _get(client)
    assert r.status_code == 403
    assert "会话级临时 token" in r.json()["detail"]["message"]


def test_takeover_repo_key_without_prefix_normalized(env):
    """兼容期无前缀 repo_key（acme/web）按签发侧默认 gitcode: 归一后放行。"""
    client, set_user = env
    set_user(_takeover_user(repo_key=f"{OWNER}/{REPO}"))
    assert _get(client).status_code == 200


def test_repo_key_scope_uses_full_repo_key(env):
    """仓级校验按完整 repo_key 严格相等（provider 前缀不同即视为跨仓）。"""
    client, set_user = env
    set_user(_takeover_user(repo_key=f"github:{OWNER}/{REPO}"))
    # 路径显式带 gitcode: 前缀 → provider=gitcode，与 tko_ 的 github: 作用域不一致
    r = client.get(f"/api/repos/gitcode:{OWNER}/{REPO}/git-credential")
    assert r.status_code == 403
    assert "仅限其任务仓库" in r.json()["detail"]["message"]