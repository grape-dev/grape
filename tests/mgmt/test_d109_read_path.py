"""D109 读路径回切（§16 #1）：表就绪 → 读表；未就绪/失败 → 回落文件+meta。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.repositories_platform_object import (
    OAuthChannelRepo,
    RepoPlatformRepo,
)
from src.domains.platform.canonical import canonical_hash


@pytest.fixture()
def engine_with_data():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    s = Session(engine)
    OAuthChannelRepo.upsert_from_config(
        s, "gitee",
        {"name": "GiteeTable", "auth_url": "https://tbl/auth", "scope": "user_info", "pkce_enabled": False},
        canonical_hash({"name": "GiteeTable"}),
    )
    # 用真实平台卡的完整字段（build_platform_card 对枚举字段 fail-closed，空串会被整卡拒绝）
    import json
    import pathlib
    raw_card = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "configs" / "platforms" / "gitee.json").read_text(encoding="utf-8")
    )
    raw_card = {k: v for k, v in raw_card.items() if not k.startswith("_")}
    raw_card.update({"name": "GiteePlatTable", "host": "tbl.gitee.io", "bot_username": "tblbot"})
    RepoPlatformRepo.upsert_from_config(s, "gitee", raw_card, canonical_hash(raw_card))
    s.commit()
    s.close()
    yield engine
    engine.dispose()


@pytest.fixture()
def table_ready(engine_with_data, monkeypatch):
    from src.kernel.persistence import engine as engine_mod
    from src.domains.platform import importer
    from src.domains.mgmt import public as mgmt_public

    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine_with_data)
    monkeypatch.setattr(importer, "is_table_backed", lambda: True)

    # 2026-11-16（三库四进程边界）：runtime 读平台卡改为走 mgmt public 跨域原语。
    # 本用例验证的是「表有行 → 卡取自表行」，所以把边界原语桩成从本 fixture 的
    # 平台对象表读（等价于 mgmt 库里已有行），不改变被测的建卡逻辑。
    def _cards(enabled_only: bool = True):
        from sqlalchemy.orm import Session

        with Session(engine_with_data) as s:
            rows = RepoPlatformRepo.list_all(s, enabled_only=enabled_only)
        return [{c.name: (getattr(r, c.name) or "") for c in r.__table__.columns}
                for r in rows]

    monkeypatch.setattr(mgmt_public, "repo_platform_cards", _cards)


def test_oauth_channels_read_from_table(table_ready):
    from src.domains.mgmt.auth import providers

    providers.invalidate_provider_cache()
    card = providers.get_provider("gitee")
    assert card is not None
    assert card["name"] == "GiteeTable"
    assert card["auth_url"] == "https://tbl/auth"


def test_repo_platform_read_from_table(table_ready):
    from src.domains.runtime.vcs import platform_card as pc

    pc.invalidate_platform_card_cache()
    card = pc.get_platform_card("gitee")
    assert card is not None
    assert card.name == "GiteePlatTable"
    assert card.host == "tbl.gitee.io"
    assert card.bot_username == "tblbot"  # 来自平台行（原 caps 字段已并入 grape_t_repo_platforms）


def test_falls_back_to_files_when_table_not_ready(monkeypatch):
    from src.domains.mgmt.auth import providers
    from src.domains.runtime.vcs import platform_card as pc
    from src.domains.platform import importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: False)
    providers.invalidate_provider_cache()
    pc.invalidate_platform_card_cache()
    assert providers.get_provider("gitee")["name"] == "Gitee"
    assert pc.get_platform_card("gitee").name == "Gitee"


def test_table_read_failure_is_fail_open(monkeypatch):
    """读表抛错 → 回落文件，不抛空、不让登录挂。"""
    from src.domains.mgmt.auth import providers
    from src.domains.platform import importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: True)

    def _boom():
        raise RuntimeError("db gone")

    from src.kernel.persistence import engine as engine_mod

    monkeypatch.setattr(engine_mod, "get_engine", _boom)
    providers.invalidate_provider_cache()
    assert providers.get_provider("gitee")["name"] == "Gitee"


def test_capabilities_include_pure_login_channel(monkeypatch):
    """能力视图必须含纯登录渠道（huawei：login=T / configured=F / hosting=F）。

    反证：known_providers() 只取「平台卡 ∪ 表内平台」时这里必红——huawei 两张表都没有。
    """
    from src.domains.mgmt.auth import providers
    from src.domains.platform import capabilities, importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: False)
    monkeypatch.setattr(providers, "list_providers", lambda: ["gitcode", "gitee", "github", "huawei"])

    caps = capabilities.all_capabilities()
    assert "huawei" in caps, f"纯登录渠道缺失，实际={sorted(caps)}"
    hw = caps["huawei"]
    assert (hw["login"], hw["configured"], hw["hosting"]) == (True, False, False)


def test_capabilities_login_falls_back_when_tables_not_ready(monkeypatch):
    """MySQL 后端第一批不建表 → 所有派生都走回落，login 不能因此全变 False。"""
    from src.domains.mgmt.auth import providers
    from src.domains.platform import capabilities, importer

    monkeypatch.setattr(importer, "is_table_backed", lambda: False)
    monkeypatch.setattr(providers, "list_providers", lambda: ["gitee", "huawei"])

    assert capabilities.platform_capabilities("gitee")["login"] is True
    assert capabilities.platform_capabilities("gitlab")["login"] is False
