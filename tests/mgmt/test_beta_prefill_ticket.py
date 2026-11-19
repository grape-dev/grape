# -*- coding: utf-8 -*-
"""白名单拦截 → 申请内测自动填充（一次性票据链路）回归。

背景：#3054 加固把第三方用户名从跳转 URL 拿掉（反射型 DOM XSS），代价是
beta-apply 页用户名预填断链。本组用例锁住替代链路：
拦截 → 服务端签发一次性票据（URL 只带 ticket）→ 申请页凭票换
{platform, username} → 单次消费 + TTL 过期 + 渠道 key 白名单。
"""
import time


def _fresh(module):
    """重载模块拿到干净票据表。"""
    import importlib

    return importlib.reload(module)


def test_issue_and_consume_roundtrip():
    from src.domains.mgmt.routes import grape_auth as ga

    ga = _fresh(ga)
    t = ga._issue_beta_prefill_ticket("gitcode", "alice_dev")
    assert t and ga._WL_PROVIDER_KEY_RE.match("gitcode")
    hit = ga._consume_beta_prefill_ticket(t)
    assert hit == ("gitcode", "alice_dev"), f"票据应换回 (provider, username): {hit}"
    # 单次消费：第二次取不到
    assert ga._consume_beta_prefill_ticket(t) is None, "票据必须一次性"


def test_unknown_ticket_is_none():
    from src.domains.mgmt.routes import grape_auth as ga

    ga = _fresh(ga)
    assert ga._consume_beta_prefill_ticket("no-such-ticket") is None
    assert ga._consume_beta_prefill_ticket("") is None


def test_ticket_ttl_expiry(monkeypatch):
    from src.domains.mgmt.routes import grape_auth as ga

    ga = _fresh(ga)
    _now = time.time()
    monkeypatch.setattr(ga._prefill_time, "time", lambda: _now)
    t = ga._issue_beta_prefill_ticket("github", "bob")
    # 时间前进 601s → 过期
    monkeypatch.setattr(ga._prefill_time, "time", lambda: _now + 601)
    assert ga._consume_beta_prefill_ticket(t) is None, "10 分钟 TTL 后票据必须失效"


def test_whitelist_error_code_carries_ticket_not_username():
    """拦截点：URL 错误码第三段是票据（服务端随机），不是第三方用户名。"""
    import inspect

    from src.domains.mgmt.routes import grape_auth as ga

    src = inspect.getsource(ga)
    assert '_wl_code += ":" + _issue_beta_prefill_ticket(provider, external_login)' in src, (
        "拦截点未签发票据 → 申请页无法自动填充")
    # 老的明文拼用户名形态必须不存在
    assert 'f"WHITELIST:{provider}:{external_login}"' not in src


def test_prefill_endpoint_exists_and_single_shot():
    from fastapi.testclient import TestClient
    from src.domains.mgmt.routes import grape_auth as ga

    ga = _fresh(ga)
    # 只挂 grape_auth 路由的轻量 app
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(ga.router)
    client = TestClient(app)
    t = ga._issue_beta_prefill_ticket("gitcode", "alice_dev")
    r1 = client.get(f"/api/auth/beta-apply/prefill?ticket={t}")
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"platform": "gitcode", "username": "alice_dev"}
    # 单次消费
    r2 = client.get(f"/api/auth/beta-apply/prefill?ticket={t}")
    assert r2.status_code == 404
    # 无效票据
    r3 = client.get("/api/auth/beta-apply/prefill?ticket=whatever")
    assert r3.status_code == 404


def test_frontend_whitelist_modal_and_apply_page_use_ticket():
    from pathlib import Path

    app_js = Path("src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    # 弹窗跳转带 prefill_ticket，不再带 username
    assert "prefill_ticket=" in app_js
    assert "'&username=' + _userQ" not in app_js
    # WHITELIST 错误码第三段按票据语义传给弹窗
    assert "_showWhitelistGuidance(parts[1] || 'gitcode', parts[2] || '')" in app_js

    vue = Path("src/api/frontend/app/src/pages/BetaApply.vue").read_text(encoding="utf-8")
    assert "params.get('prefill_ticket')" in vue, "申请页未取票据参数"
    assert "/api/auth/beta-apply/prefill" in vue, "申请页未凭票换预填"
