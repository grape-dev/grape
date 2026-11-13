"""平台可见性开关：vcs_platforms_hidden（D102-N 续，2026-11-08 用户裁定）。

引导页/凭据页不应展示本部署未启用的平台（实例：GitLab）。管理面通过
meta `vcs_platforms_hidden`（JSON 数组，provider 名单）隐藏平台目录条目；
缺省空数组 = 全部可见（向前兼容）。隐藏只影响目录接口消费端（引导页/
凭据页渲染），不影响既有 repo_key 数据读写。
"""
import json
from types import SimpleNamespace

from src.domains.mgmt.user import db as user_db
from src.domains.runtime.vcs import platform_card
from src.domains.mgmt.routes import settings as settings_routes


def _fake_user():
    return {"id": 1, "username": "t", "org_id": 0}


def test_hidden_platforms_filtered_from_catalog(monkeypatch):
    def fake_meta(k):
        if k == "vcs_platforms_hidden":
            return json.dumps(["gitlab"])
        if k == "oauth_gitee_client_id":
            return "cfg"
        return ""
    monkeypatch.setattr(user_db, "get_meta_value", fake_meta)
    out = settings_routes.list_vcs_platforms_public()
    providers = [p["provider"] for p in out["platforms"]]
    assert "gitlab" not in providers, "隐藏名单平台仍出现在目录"
    assert "gitcode" in providers and "gitee" in providers


def test_default_platforms_always_visible(monkeypatch):
    """GitCode/GitHub = 默认平台：无论 OAuth 是否配置恒显示（用户裁定）。"""
    monkeypatch.setattr(user_db, "get_meta_value", lambda k: "")
    out = settings_routes.list_vcs_platforms_public()
    providers = [p["provider"] for p in out["platforms"]]
    assert "gitcode" in providers and "github" in providers, "默认平台被过滤"


def test_non_default_platform_visible_without_oauth(monkeypatch):
    """非默认平台（gitee/gitlab）：可见性唯一权威 = vcs_platforms_hidden。

    weekly-11 P1-8 口径（2026-11-12）：撤掉「非默认平台须配 OAuth 才显示」的
    隐式白名单——纯 PAT 部署（不配 OAuth）也必须能看到平台卡、才能绑定 PAT。
    旧口径（2026-11-08 裁定）想藏未启用平台，现由显式 vcs_platforms_hidden 承担。
    """
    def fake_meta(k):
        if k == "vcs_platforms_hidden":
            return "[]"
        if k.startswith("oauth_") and k.endswith("_client_id"):
            return "cfg" if k == "oauth_gitee_client_id" else ""
        return ""
    monkeypatch.setattr(user_db, "get_meta_value", fake_meta)
    out = settings_routes.list_vcs_platforms_public()
    providers = [p["provider"] for p in out["platforms"]]
    assert "gitee" in providers and "gitlab" in providers, "非隐藏平台应全部可见（含未配 OAuth 的）"


def test_empty_hidden_list_keeps_all(monkeypatch):
    monkeypatch.setattr(user_db, "get_meta_value", lambda k: "" )
    out = settings_routes.list_vcs_platforms_public()
    providers = [p["provider"] for p in out["platforms"]]
    assert set(providers) == {"gitcode", "gitee", "github", "gitlab"}, \
        "隐藏名单为空 = 全部平台卡可见（可见性唯一权威）"


def test_hidden_list_tolerates_bad_json(monkeypatch):
    def fake_meta(k):
        if k == "vcs_platforms_hidden":
            return "not-json["
        if k == "oauth_gitee_client_id":
            return "cfg"
        return ""
    monkeypatch.setattr(user_db, "get_meta_value", fake_meta)
    out = settings_routes.list_vcs_platforms_public()
    assert len(out["platforms"]) >= 3, "非法隐藏名单应视为空（不炸目录）"
