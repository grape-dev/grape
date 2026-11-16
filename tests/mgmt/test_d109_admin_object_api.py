"""D109 管理面对象式 API（UC-D109-09）：保存同步写表 + 路由注册。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.domains.mgmt.auth.credential_crypto import decrypt
from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo, RepoPlatformRepo
from src.domains.platform import admin_sync, importer


@pytest.fixture()
def engine_with_channels():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    s = Session(engine)
    OAuthChannelRepo.upsert_from_config(s, "gitee", {"name": "Gitee"}, "h-gitee")
    OAuthChannelRepo.upsert_from_config(s, "gitcode", {"name": "GitCode"}, "h-gc")
    RepoPlatformRepo.upsert_from_config(s, "gitcode", {"name": "GitCode"}, "p-gc")
    RepoPlatformRepo.upsert_from_config(s, "github", {"name": "GitHub"}, "p-gh")
    s.commit()
    s.close()
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def wiring(engine_with_channels, monkeypatch):
    from src.kernel.persistence import engine as engine_mod

    _meta = {}
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "get_meta_value", lambda k: _meta.get(k, ""), raising=False)
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: _meta.__setitem__(k, v), raising=False)
    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine_with_channels)
    # uow 模块是模块级 `from ... import get_engine`，必须同时 patch 其命名空间，
    # 否则组合跑（uow 已被更早的测试 import）会绑定到真实库 engine。
    from src.kernel.persistence import uow as uow_mod

    monkeypatch.setattr(uow_mod, "get_engine", lambda: engine_with_channels, raising=False)
    # 缓存失效是 best-effort，测试里不需要真模块
    return _meta


def test_admin_save_syncs_bot_token_and_oauth_credentials(engine_with_channels):
    admin_sync.sync_from_admin_payload({
        "gitcode_api_token": "gc-tok",
        "bot_usernames": ["bot_a"],
        "gitee_oauth_client_id": "gid",
        "gitee_oauth_client_secret": "gsec",
    })
    with Session(engine_with_channels) as s:
        acc = RepoPlatformRepo.get(s, "gitcode")
        assert acc is not None and acc.bot_token_enc
        assert decrypt(acc.bot_token_enc) == "gc-tok"  # 加密落库、可解密
        ch = OAuthChannelRepo.get(s, "gitee")
        assert decrypt(ch.client_id_enc) == "gid"
        assert decrypt(ch.client_secret_enc) == "gsec"
    assert importer.current_epoch() == 1


def test_admin_save_is_fail_safe_for_missing_channel():
    # 不存在的渠道只告警跳过，不抛（旧保存路径已完成）
    admin_sync.sync_from_admin_payload({"huawei_oauth_client_secret": "x"})


def _iter_route_paths(routes, prefix=""):
    """展开 FastAPI 0.133 的惰性 _IncludedRouter。

    `router.routes` 里子路由是 `_IncludedRouter` 占位对象（无 `.path`），
    需连同 include 上下文里的 prefix 递归展开才能拿到最终路径。
    """
    for r in routes:
        ctx = getattr(r, "include_context", None)
        if ctx is not None:  # _IncludedRouter
            yield from _iter_route_paths(r.original_router.routes,
                                         prefix + (ctx.prefix or ""))
            continue
        p = getattr(r, "path", None)
        if p:
            yield prefix + p


def test_object_api_routes_are_registered():
    from src.domains.mgmt.admin.routes import router

    paths = set(_iter_route_paths(router.routes))
    # 五聚合 + 视图，一个对象一个集合（d109 §19；credential-caps 随 repo-credentials.json 删除）
    for expect in (
        "/api/admin/oauth-channels",
        "/api/admin/oauth-channels/{channel}",
        "/api/admin/oauth-channels/{channel}/credentials",
        "/api/admin/oauth-channels/{channel}/enabled",
        "/api/admin/repo-platforms",
        "/api/admin/repo-platforms/{provider}",
        "/api/admin/repo-platforms/{provider}/enabled",
        "/api/admin/channel-repo-links",
        "/api/admin/channel-repo-links/{oauth_provider}/{repo_provider}",
        "/api/admin/user-repo-platform-links",
        "/api/admin/repo-platforms/{provider}/bot",
        "/api/admin/capabilities",
        "/api/admin/user-platforms",
    ):
        assert expect in paths, f"缺路径 {{expect}}；实际={{sorted(paths)}}"
    # 禁止项：无虚构父容器、无整卡 PUT
        f"出现裸名路径: {{sorted(p for p in paths if 'platforms/' in p or 'channels' in p)}}"
