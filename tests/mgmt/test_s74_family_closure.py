"""S74 用例（D49，先测后改）：剩余挂账收口——附件同步评论卡化、落库静默
失败响亮化、config-only 登录渠道管理面。

- 附件同步评论（issues_center _attachment_sync_comment）：原 `if _prov !=
  "gitcode": return False` 把 gitee 等平台附件同步整族静默跳过；
  token 取 get_gitcode_token（gitcode 专用列）→ 卡化 provider + 
  get_repo_token_with_refresh（provider 感知策略链）。
- add_managed_repo：原非法列/SQL 失败静默 return False（S64 事故形状——
  10 调用点零检查，"托管成功但未落库"）→ raise 化（调用方全部在
  try/except 内，异常路由进 Issue #671 兜底自愈，行为语义=响亮失败）。
- 登录渠道管理面（E 族）：oauth_providers_config 无 admin 路由——
  config-only 渠道（gitee OAuth）只能直写 DB。补 GET/PUT /auth-channels。
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth.providers import invalidate_provider_cache
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture(autouse=True)
def _clean_meta():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ------------------------------------------------ 附件同步评论卡化

def test_s74_attachment_sync_comment_gitee(gitee_card):
    """gitee 仓附件同步评论：VcsApiClient(provider=gitee) + provider 感知
    token（原 gitcode 门禁 + gitcode 专用 token 列 → 整族静默跳过）。"""
    from src.api.issues_center import sync_issue_attachment_comment
    calls = []

    def _fake_create(self, title, body="", assignee=""):
        return {"number": 1}

    def _fake_post(self, issue_number, body):
        calls.append({"provider": self.provider if hasattr(self, "provider") else None,
                      "owner": self.owner, "repo": self.repo,
                      "issue": issue_number, "body": body})
        return {"id": 9}

    att = {"url": "http://obs/a.png", "filename": "a.png", "name": "a.png"}
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="gt-1") as m_tok, \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.post_issue_comment", _fake_post), \
         patch("src.api.issues_center._attachments_markdown_block",
               return_value="![a](http://obs/a.png)"), \
         patch("src.domains.runtime.storage.get_storage", return_value=None):
        ok = sync_issue_attachment_comment("gitee:acme/web", 701, att)
    assert ok is True, "gitee 附件同步评论应成功（原被 gitcode 门禁静默跳过）"
    assert calls, "评论未发出"
    m_tok.assert_called_once_with("gitee:acme/web")
    assert calls[0]["provider"] == "gitee", (
        f"provider 应按 repo_key 派生为 gitee，got {calls[0].get('provider')!r}")
    assert calls[0]["owner"] == "acme" and calls[0]["repo"] == "web"


# ------------------------------------------------ add_managed_repo 响亮失败

def test_s74_add_managed_repo_raises_on_illegal_column():
    """非法列名 → raise（原静默 return False，10 调用点零检查——S64 事故形状）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key='s74:o/r'")
        db.commit()
    with pytest.raises((ValueError, RuntimeError)):
        add_managed_repo("s74:o/r", "o", "r", **{"bad-col!": "x"})


def test_s74_add_managed_repo_raises_on_sql_failure():
    """SQL 硬失败（连接异常）→ raise 而非静默 False（托管流 except 路由 #671 兜底）。
    注：未知列走"列不存在降级"兜底（兼容未迁移库的有意设计，S74 补响亮日志）。"""
    import src.domains.mgmt.user.db as udb
    from src.domains.mgmt.user.db import add_managed_repo

    class _Boom:
        def cursor(self):
            raise RuntimeError("sql boom")

    with patch.object(udb, "db", _Boom()), \
         pytest.raises(RuntimeError):
        add_managed_repo("s74:o3/r", "o3", "r", webhook_id=1)


# ------------------------------------------------ 登录渠道管理面（E 族）

def _admin_client():
    from src.api.main import app
    c = TestClient(app)
    r = c.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


def test_s74_admin_auth_channels_roundtrip():
    """GET/PUT /api/admin/auth-channels：渠道卡保存（合并式——不挤掉既有
    gitcode/github 渠道）+ client 凭据写 oauth_{p}_client_id/secret meta。"""
    c = _admin_client()
    # 本用例会写 meta（渠道卡 + oauth_gitee_client_id/secret）——必须快照/恢复，
    # 否则泄漏到后续用例：/api/settings/vcs/platforms 的可见性判定按
    # oauth_<p>_client_id 派生，残留 cid-1 会让 gitee 恒显（实测
    # test_bot_identity_cli_catalog::test_f1 组合挂，隔离绿）。
    _keys = ("oauth_gitee_client_id", "oauth_gitee_client_secret", "oauth_providers_config")
    _snapshot = {}
    with db_lock:
        _c = db.cursor()
        for _k in _keys:
            _row = _c.execute("SELECT value FROM meta WHERE key=?", (_k,)).fetchone()
            _snapshot[_k] = _row[0] if _row else None
    try:
        _run_auth_channels_roundtrip(c)
    finally:
        from src.domains.mgmt.auth.providers import invalidate_provider_cache
        with db_lock:
            _c = db.cursor()
            for _k, _v in _snapshot.items():
                if _v is None:
                    _c.execute("DELETE FROM meta WHERE key=?", (_k,))
                else:
                    _c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_k, _v))
            db.commit()
        invalidate_provider_cache()


def _run_auth_channels_roundtrip(c):
    r = c.put("/api/admin/auth-channels", json={
        "providers": {
            "gitee": {
                "name": "Gitee", "type": "oauth2",
                "auth_url": "https://gitee.com/oauth/authorize",
                "token_url": "https://gitee.com/oauth/token",
                "user_url": "https://gitee.com/api/v5/user",
                "scope": "user_info", "id_field": "id", "login_field": "login",
                "name_field": "name", "pkce_enabled": False,
                "brand_color": "#c71d23", "accept_header": "application/json",
                "client_id": "cid-1", "client_secret": "csec-1",
            }}})
    assert r.status_code == 200, r.text
    # 合并式：gitcode/github 默认渠道仍在
    r2 = c.get("/api/admin/auth-channels")
    assert r2.status_code == 200, r2.text
    providers = {p["provider"]: p for p in r2.json().get("providers", [])}
    assert "gitee" in providers and providers["gitee"].get("configured") is True
    assert {"gitcode", "github"} <= set(providers), (
        f"保存 gitee 渠道不得挤掉存量渠道（覆盖式写入），got {list(providers)}")
    # 凭据落 meta（协议卡不含凭据）
    with db_lock:
        cid = db.cursor().execute(
            "SELECT value FROM meta WHERE key='oauth_gitee_client_id'").fetchone()
        card_raw = db.cursor().execute(
            "SELECT value FROM meta WHERE key='oauth_providers_config'").fetchone()
    assert cid and cid[0] == "cid-1"
    card = json.loads(card_raw[0]) if card_raw else {}
    assert "client_secret" not in (card.get("gitee") or {}), "凭据不得进协议卡"


def test_s74_admin_auth_channels_invalid_card_rejected():
    """非法渠道卡（缺 token_url）→ 400 带字段名（S45 构建即校验语义）。"""
    c = _admin_client()
    r = c.put("/api/admin/auth-channels", json={
        "providers": {"bad_ch": {"name": "Bad", "type": "oauth2",
                                 "auth_url": "https://x/authorize"}}})
    assert r.status_code == 400, r.text
