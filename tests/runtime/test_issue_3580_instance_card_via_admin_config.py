"""Issue #3580（S1/S2）：自建 GitLab 实例卡经既有管理接口接入。

不新增平台对象写接口，也不新增前端页面——实例卡走既有
`PUT /api/admin/vcs-platforms`（Builder 全字段校验 + host/webhook_path 全局唯一 + 审计），
本文件锁三件事：
1. 合法卡写入后：meta 有该卡、平台卡可解析、用户面平台目录可见；
2. 非法卡（api_base 非 http / ca_bundle 相对路径 / dialect 未知）400，且**库内零写入**；
3. host / webhook_path 与既有平台冲突时 400（一实例一 host/路径的不变量）。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from src.domains.mgmt.admin.routes import VcsPlatformCardBody, vcs_platforms_put
from src.domains.mgmt.user.db import get_meta_value
from src.domains.runtime.vcs.platform_card import (
    get_platform_card, invalidate_platform_card_cache,
)
from src.kernel.session_manager import db, db_lock

INSTANCE = "gitlab-corp"


def _raw(**over) -> dict:
    raw = {
        "name": "GitLab Corp", "dialect": "gitlab", "host": "gitlab.corp.example.com",
        "webhook_path": f"/webhook/{INSTANCE}", "event_header": "X-Gitlab-Event",
        "api_base": "https://gitlab.corp.example.com/api/v4",
        "api_auth": "private_token", "webhook_verify": "token_compare",
        "clone_auth": "oauth2", "webhook_dialect": "gitlab_v4",
        "download_base": "https://gitlab.corp.example.com",
    }
    raw.update(over)
    return raw


def _cards_meta() -> dict:
    with db_lock:
        row = db.cursor().execute(
            "SELECT value FROM meta WHERE key='vcs_providers_config'").fetchone()
    return json.loads(row[0]) if row and row[0] else {}


@pytest.fixture(autouse=True)
def _clean_meta():
    def _wipe():
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()

    _wipe()
    yield
    _wipe()


def test_valid_instance_card_is_written_and_visible():
    body = VcsPlatformCardBody(cards={INSTANCE: _raw()})
    res = vcs_platforms_put(body, admin={"id": 1, "username": "admin"})

    assert res["cards"] == [INSTANCE]
    assert INSTANCE in _cards_meta()
    invalidate_platform_card_cache()
    card = get_platform_card(INSTANCE)
    assert card is not None and card.api_base.endswith("/api/v4")
    assert card.webhook_path == f"/webhook/{INSTANCE}"

    # 用户面平台目录（表未就绪/表空时回落平台卡）：实例必须出现
    from src.domains.mgmt.routes.settings import list_vcs_platforms_public

    visible = list_vcs_platforms_public()
    providers = {str(row.get("provider")) for row in visible["platforms"]}
    assert INSTANCE in providers, f"实例未出现在 /settings/vcs/platforms: {visible}"


def test_instance_card_rejected_when_fields_invalid():
    for bad in ({"api_base": "ftp://gl.corp/api/v4"},          # 非 http(s)
                {"ca_bundle": "certs/ca.pem"},                 # 相对路径
                {"dialect": "unknown_dialect"}):               # 未知方言
        body = VcsPlatformCardBody(cards={INSTANCE: _raw(**bad)})
        with pytest.raises(HTTPException) as ei:
            vcs_platforms_put(body, admin={"id": 1})
        assert ei.value.status_code == 400
        assert "platform card validation failed" in str(ei.value.detail)
        assert INSTANCE not in _cards_meta(), f"非法卡不得写入库: {bad}"


def test_duplicate_host_rejected():
    """一实例一 host：与内置 gitlab（gitlab.com）撞 host 必须拒。"""
    body = VcsPlatformCardBody(cards={
        "gitlab": {"name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
                   "webhook_path": "/webhook/gitlab", "event_header": "X-Gitlab-Event",
                   "api_base": "https://gitlab.com/api/v4", "api_auth": "private_token",
                   "webhook_verify": "token_compare", "clone_auth": "oauth2",
                   "webhook_dialect": "gitlab_v4"},
        INSTANCE: _raw(host="gitlab.com"),
    })
    with pytest.raises(HTTPException) as ei:
        vcs_platforms_put(body, admin={"id": 1})
    assert ei.value.status_code == 400
    assert "host" in str(ei.value.detail)
    assert INSTANCE not in _cards_meta()


def test_secret_channels_are_separate_from_card():
    """bot / webhook token 走独立通道，不进卡（Builder 会拒未知键）。"""
    body = VcsPlatformCardBody(cards={INSTANCE: _raw()},
                               bot_tokens={INSTANCE: "glpat-bot"}, webhook_tokens={INSTANCE: "whsec"})
    vcs_platforms_put(body, admin={"id": 1})
    cards = _cards_meta()
    assert INSTANCE in cards
    assert "glpat-bot" not in json.dumps(cards, ensure_ascii=False)
    assert get_meta_value("global_bot_gitlab-corp_token") == "glpat-bot"
    assert get_meta_value("gitlab-corp_webhook_token") == "whsec"