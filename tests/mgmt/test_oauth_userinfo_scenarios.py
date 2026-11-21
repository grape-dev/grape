"""场景用例代码：OAuth userinfo 端点与字段口径（华为渠道，S1–S7）。

对应用例表：`docs/weekly-11/oauth-userinfo-endpoint-design.md` §4。
现场：换 code 成功、拉用户信息 500（端点错）→ 回调 ?oauth_error=user_info_failed；
另两处配置错（name_field=nickName、scope=openid）不报错但昵称恒空。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CARD = {
    "name": "Huawei", "type": "oauth2",
    "auth_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize",
    "token_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
    "user_url": "https://account.cloud.huawei.com/rest.php?nsp_svc=GOpen.User.getInfo"
                "&nsp_fmt=JSON&getNickName=1&access_token={access_token}",
    "scope": "openid profile email",
    "id_field": "openID", "login_field": "", "name_field": "displayName",
    "avatar_field": "headPictureURL", "pkce_enabled": False,
}


class _Resp:
    def __init__(self, status: int, body, text: str = ""):
        self.status_code = status
        self._body = body
        self.text = text or (json.dumps(body) if isinstance(body, (dict, list)) else str(body))

    def json(self):
        return self._body

    def raise_for_status(self):
        """模拟 requests：>=400 抛异常（改造后的代码先看 status、不再依赖它）。"""
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Server Error: simulated for url")


@pytest.fixture()
def wired(monkeypatch):
    """接管渠道卡与 requests.get，记录调用参数。"""
    from src.domains.mgmt.auth import providers as P

    calls: list[dict] = []

    def _fake_get(url, headers=None, timeout=None, proxies=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return _Resp(200, {"openID": "id-1", "displayName": "Zhang San",
                           "headPictureURL": "https://example.invalid/a.png"})

    monkeypatch.setattr(P, "get_provider", lambda provider: dict(CARD))
    monkeypatch.setattr(P, "_channel_emails_endpoint", lambda provider, cfg: "")
    monkeypatch.setattr("requests.get", _fake_get)
    return calls


def _info(provider="huawei", token="tk-abc/+= "):
    from src.domains.mgmt.auth.providers import get_user_info
    return get_user_info(provider, token)


def test_s1_token_in_url_skips_bearer(wired):
    """S1：{access_token} 占位 → token 进查询参数（urlencode），且不发 Authorization。"""
    info = _info(token="a b+c")
    assert info and info["external_id"] == "id-1"
    url = wired[0]["url"]
    assert "access_token=" in url and "a%20b%2Bc" in url, url
    assert "Authorization" not in wired[0]["headers"], "占位命中时不得再发 Bearer（华为会拒）"


def test_s2_bearer_when_no_placeholder(monkeypatch, wired):
    """S2：无占位 → 发 Authorization: Bearer。"""
    from src.domains.mgmt.auth import providers as P
    card = dict(CARD, user_url="https://api.github.invalid/user")
    monkeypatch.setattr(P, "get_provider", lambda provider: card)
    info = _info(provider="github", token="tk123")
    assert info["external_id"] == "id-1"
    assert wired[0]["headers"].get("Authorization") == "Bearer tk123"


def test_s3_name_field_hit(wired):
    """S3：name_field=displayName 命中 → external_name 取该字段，不触发回落告警。"""
    info = _info()
    assert info["external_name"] == "Zhang San"


def test_s4_name_field_miss_falls_back(monkeypatch, wired, caplog):
    """S4：卡上 name_field 配错（nickName）→ 回落 displayName 并告警（不静默空名）。"""
    from src.domains.mgmt.auth import providers as P
    card = dict(CARD, name_field="nickName")
    monkeypatch.setattr(P, "get_provider", lambda provider: card)
    with caplog.at_level(logging.WARNING):
        info = _info()
    assert info["external_name"] == "Zhang San", "配错字段名时必须回落，而不是空串"
    hits = [r for r in caplog.records if "疑似字段名配错" in r.getMessage()]
    assert len(hits) == 1, f"应有且仅一条回落告警，实际 {len(hits)}"


def test_s5_userinfo_500_is_diagnosable(wired, monkeypatch, caplog):
    """S5：userinfo 500 → 返回 None；日志带状态码、打码端点、响应片段，且不含 token。"""
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _Resp(500, {}, text="<html>error</html>"))
    with caplog.at_level(logging.WARNING):
        info = _info(token="SECRET_TOKEN_3563")
    assert info is None
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTP 500" in msg and "account.cloud.huawei.com" in msg
    assert "access_token=***" in msg, "端点要能定位，但 token 必须打码"
    assert "SECRET_TOKEN_3563" not in msg, "token 不得进日志"


def test_s6_missing_avatar_is_empty(monkeypatch, wired):
    """S6：avatar_field 指向不存在的键 → 空头像，不抛。"""
    from src.domains.mgmt.auth import providers as P
    card = dict(CARD, avatar_field="notThere")
    monkeypatch.setattr(P, "get_provider", lambda provider: card)
    info = _info()
    assert info["external_avatar"] == ""


def test_s7_channel_cards_and_scope(monkeypatch):
    """S7：两个配置文件 huawei 三字段正确；build_auth_url 的跳转带 scope。"""
    for rel in ("configs/oauth-providers.json", "configs/oauth-providers.example.json"):
        h = json.loads((ROOT / rel).read_text(encoding="utf-8"))["huawei"]
        assert h["user_url"].startswith("https://account.cloud.huawei.com/rest.php")
        assert "{access_token}" in h["user_url"]
        assert "GOpen.User.getInfo" in h["user_url"]
        assert h["scope"] == "openid profile email"
        assert h["name_field"] == "displayName"

    from src.domains.mgmt.auth import providers as P
    monkeypatch.setattr(P, "get_provider", lambda provider: dict(CARD))
    monkeypatch.setattr(P, "_get_client_id", lambda provider: "cid-3563")
    monkeypatch.setattr(P.user_db, "save_oauth_state", lambda *a, **k: None)
    out = P.build_auth_url("huawei", "state-1", "https://example.invalid/api/auth/callback/huawei")
    assert out and "scope=" in out["redirect_url"]
    _u = out["redirect_url"]
    assert ("scope=openid+profile+email" in _u
            or urllib.parse.quote("openid profile email", safe="") in _u), _u