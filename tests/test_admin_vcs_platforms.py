"""S18：VCS 平台卡管理面 API（admin CRUD + 校验 + 热失效）。"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache, get_platform_card


def _admin_client():
    """登录 admin（默认账号由 db_setup 建的 admin/admin123，测试库专用）。"""
    from src.api.main import app
    c = TestClient(app)
    r = c.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _cleanup_meta():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


GITEE = {"dialect": "github", "host": "gitee.com", "api_auth": "query_token"}


def test_s18_put_capability_fields_roundtrip():
    """S70（实弹发现）：能力位字段（issue_write_form 等整族）经 admin 保存必须回读不丢——
    原白名单清洗丢字段 → gitee 卡落回 repo_path → 建单 404（静默降级）。"""
    _cleanup_meta()
    c = _admin_client()
    card = dict(GITEE)
    card.update({
        "issue_write_form": "owner_param",       # D41/S61
        "branch_create_form": "form",            # D44/S66
        "user_images_base": "https://assets.gitee.com/user-images",
        "download_base": "https://gitee.com",
        "issue_search": False,
        "token_storage_field": "bot_gitee_token",
        "webhook_token_field": "gitee_hook_token",
        "session_token_field": "gitee_session_token",
        "pat_create_url": "https://gitee.com/profile/personal_access_tokens",
    })
    r = c.put("/api/admin/vcs-platforms", json={"cards": {"gitee": card}})
    assert r.status_code == 200, r.text
    _c = get_platform_card("gitee")
    assert _c is not None
    assert _c.issue_write_form == "owner_param", (
        f"issue_write_form lost on save: {_c.issue_write_form!r}")
    assert _c.branch_create_form == "form"
    assert _c.user_images_base == "https://assets.gitee.com/user-images"
    assert _c.download_base == "https://gitee.com"
    assert _c.token_storage_field == "bot_gitee_token"
    assert _c.webhook_token_field == "gitee_hook_token"
    assert _c.session_token_field == "gitee_session_token"
    assert _c.pat_create_url == "https://gitee.com/profile/personal_access_tokens"


def test_s18_list_contains_builtin_cards():
    c = _admin_client()
    r = c.get("/api/admin/vcs-platforms")
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert {"gitcode", "github", "gitlab"} <= set(cards)
    assert cards["gitcode"]["host"] == "gitcode.com"


def test_s18_put_valid_card_and_hot_effect():
    _cleanup_meta()
    c = _admin_client()
    r = c.put("/api/admin/vcs-platforms", json={"cards": {"gitee": GITEE}})
    assert r.status_code == 200, r.text
    assert r.json()["cards"] == ["gitee"]
    # 热失效：无需重启即可取到
    assert get_platform_card("gitee") is not None
    assert get_platform_card("gitee").host == "gitee.com"
    # S83：meta 清除后 gitee 回落文件内置默认（不再是 config-only 消失）
    _cleanup_meta()
    _g = get_platform_card("gitee")
    assert _g is not None and _g.host == "gitee.com"
    assert _g.issue_write_form == "owner_param"  # 文件能力位完整


def test_s18_put_invalid_card_rejected_with_errors():
    _cleanup_meta()
    c = _admin_client()
    r = c.put("/api/admin/vcs-platforms", json={"cards": {
        "bad1": {"dialect": "svn", "host": "x.com"},
        "bad2": {"host": "y.com", "token_env": "not-upper"},
    }})
    assert r.status_code == 400
    errs = r.json()["detail"]["errors"]
    assert any("bad1" in e and "dialect" in e for e in errs)
    assert any("bad2" in e and "token_env" in e for e in errs)
    # 非法卡未落库
    assert get_platform_card("bad1") is None
    _cleanup_meta()


def test_s18_put_duplicate_host_rejected():
    _cleanup_meta()
    c = _admin_client()
    r = c.put("/api/admin/vcs-platforms", json={"cards": {
        "p1": {"dialect": "github", "host": "dup.example.com"},
        "p2": {"dialect": "github", "host": "dup.example.com", "webhook_path": "/webhook/p2"},
    }})
    assert r.status_code == 400
    assert any("host" in e and "重复" in e for e in r.json()["detail"]["errors"])
    _cleanup_meta()


def test_s18_requires_admin():
    from src.api.main import app
    c = TestClient(app)
    assert c.get("/api/admin/vcs-platforms").status_code in (401, 403)
