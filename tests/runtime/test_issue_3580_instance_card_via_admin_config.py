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


@pytest.fixture()
def orm_engine(monkeypatch):
    """平台对象表（D109）在内存 sqlite 里就绪，并接管路由里的 get_engine()。"""
    from sqlalchemy import create_engine

    from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL

    eng = create_engine("sqlite://", future=True)
    raw = eng.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    import src.kernel.persistence.engine as _eng_mod

    monkeypatch.setattr(_eng_mod, "get_engine", lambda: eng)

    # A 方案（表为真源）：测试必须让「表就绪」成立，否则卡装配会走降级路径。
    from src.domains.platform import importer as _imp

    monkeypatch.setattr(_imp, "is_table_backed", lambda: True)

    from src.kernel.gsb import provider as _gsb

    class _MgmtSvc:
        @staticmethod
        def get_repo_platform_cards(enabled_only=True):
            from sqlalchemy.orm import Session

            from src.kernel.persistence.models_platform_object import RepoPlatformModel
            from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

            with Session(eng) as session:
                rows = RepoPlatformRepo.list_all(session, enabled_only=enabled_only)
                cols = [c.name for c in RepoPlatformModel.__table__.columns]
                return [{k: getattr(r, k, None) for k in cols} for r in rows]

    monkeypatch.setattr(_gsb, "get_service", lambda name: _MgmtSvc())
    return eng


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


def test_valid_instance_card_is_written_and_visible(orm_engine):
    body = VcsPlatformCardBody(cards={INSTANCE: _raw()})
    res = vcs_platforms_put(body, admin={"id": 1, "username": "admin"})

    assert res["cards"] == [INSTANCE]
    # A 方案：卡定义落表，不再写 meta `vcs_providers_config`
    assert INSTANCE not in _cards_meta()
    invalidate_platform_card_cache()
    assert get_platform_card(INSTANCE) is not None
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


def test_secret_channels_are_separate_from_card(orm_engine):
    """bot / webhook token 走独立通道，不进卡（Builder 会拒未知键）。"""
    body = VcsPlatformCardBody(cards={INSTANCE: _raw()},
                               bot_tokens={INSTANCE: "glpat-bot"}, webhook_tokens={INSTANCE: "whsec"})
    vcs_platforms_put(body, admin={"id": 1})
    from dataclasses import asdict

    invalidate_platform_card_cache()
    card = get_platform_card(INSTANCE)
    assert card is not None
    assert "glpat-bot" not in json.dumps(asdict(card), ensure_ascii=False)
    assert get_meta_value("global_bot_gitlab-corp_token") == "glpat-bot"
    assert get_meta_value("gitlab-corp_webhook_token") == "whsec"

# ---------------- 最小配置（方言默认值）与平台行落库 ----------------

def test_minimal_card_derives_gitlab_defaults(orm_engine):
    """只给 dialect + host：其余按 GitLab 方言默认派生（B 方案）。"""
    body = VcsPlatformCardBody(cards={INSTANCE: {"dialect": "gitlab",
                                                 "host": "gitlab.corp.example.com"}})
    res = vcs_platforms_put(body, admin={"id": 1})
    assert res["rows"] == {INSTANCE: "inserted"}
    assert "rows_error" not in res

    invalidate_platform_card_cache()
    card = get_platform_card(INSTANCE)
    assert card.api_base == "https://gitlab.corp.example.com/api/v4"
    assert card.api_auth == "private_token"
    assert card.clone_auth == "oauth2"
    assert card.webhook_dialect == "gitlab_v4"
    assert card.event_header == "X-Gitlab-Event"
    assert card.webhook_path == f"/webhook/{INSTANCE}"
    assert card.download_base == "https://gitlab.corp.example.com"


def test_instance_row_is_manual_and_listed(orm_engine):
    body = VcsPlatformCardBody(cards={INSTANCE: {"dialect": "gitlab",
                                                 "host": "gitlab.corp.example.com"}})
    vcs_platforms_put(body, admin={"id": 1})

    from sqlalchemy.orm import Session

    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    with Session(orm_engine) as s:
        row = RepoPlatformRepo.get(s, INSTANCE)
        assert row is not None
        assert str(row.source) == "manual"          # 导入器永不覆盖、mark_orphaned 不会软删
        assert int(row.enabled) == 1
        assert str(row.api_base) == "https://gitlab.corp.example.com/api/v4"
        assert str(row.host) == "gitlab.corp.example.com"
        # Issue #3580 实测：手抄白名单曾漏掉这两个 → 托管 fail-closed / 私有 CA 不生效
        assert str(row.webhook_dialect) == "gitlab_v4"
        assert str(row.branch_create_form) != ""      # asdict 全字段，不再漏能力位

    from src.domains.mgmt.routes.settings import list_vcs_platforms_public

    providers = {str(p.get("provider")) for p in list_vcs_platforms_public()["platforms"]}
    assert INSTANCE in providers                  # 目录立即可见（不需要重启）

    # A 方案：管理面接管的事实由表行 source 表达（前端据此显示标记，不再读 meta）
    from src.domains.mgmt.admin.platform_cards.service import PlatformCardService

    views, _checked = PlatformCardService().list_cards()
    assert views[INSTANCE].managed_by == "table"
    assert views["gitcode"].managed_by == "file"     # 文件派生、未被管理面接管


def test_admin_save_promotes_file_backed_row(orm_engine):
    """A 方案：管理面是唯一写入口——config 行被管理面保存后提升为 manual。

    旧语义（config_owned 拦写）保护文件真源，代价是管理面改不动内建平台、
    只能靠 meta 覆盖层，最终形成「文件/meta/表」三真源。改为：一旦管理面保存，
    该行转 manual，导入器从此不再回灌；enabled/bot 凭据仍由各自入口保护。
    """
    from sqlalchemy.orm import Session

    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    with Session(orm_engine) as s:
        RepoPlatformRepo.upsert_from_config(
            s, "gitcode", {"name": "GitCode", "host": "gitcode.com"}, "hash-config")
        s.commit()

    body = VcsPlatformCardBody(cards={"gitcode": {
        "name": "GitCode-Edited", "dialect": "github", "host": "gitcode.com",
        "api_base": "https://gitcode.com/api/v5", "api_auth": "query_token",
        "webhook_path": "/webhook/gitcode", "event_header": "X-GitCode-Event",
        "webhook_verify": "hmac", "webhook_dialect": "gitee_v5", "clone_auth": "x_access_token"}})
    with Session(orm_engine) as s:
        RepoPlatformRepo.set_enabled(s, "gitcode", False)   # 保护字段：管理面保存不得改它
        s.commit()

    res = vcs_platforms_put(body, admin={"id": 1})
    assert res["rows"] == {"gitcode": "promoted"}

    with Session(orm_engine) as s:
        row = RepoPlatformRepo.get(s, "gitcode")
        assert str(row.source) == "manual"          # 管理面接管，导入器不再回灌
        assert str(row.name) == "GitCode-Edited"    # 管理面改动已落表
        assert int(row.enabled) == 0                # enabled 属保护字段，不被卡保存覆盖
