"""D109 导入/扫描服务：split mode 六类判定（UC-D109-01..09/16/25/26）。

覆盖：
- 首次导入 → 幂等（第二次全 skipped 且 epoch 不变）
- 改内容 → updated；删文件 → orphaned 软删；orphaned 原样加回 → 恢复
- manual 行不被覆盖
- 未知配置键 → 该 provider failed、其他 provider 照常导入
- 纯登录渠道不派生幽灵 link；显式 link 指向不存在平台 → failed
- preview 不写库
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.models_platform_object import OAuthChannelModel, RepoPlatformModel
from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo
from src.domains.platform import importer
from src.domains.platform.canonical import canonical_hash

_RC_DDL = (
    "CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
    " id VARCHAR(256) PRIMARY KEY, repo_key TEXT NOT NULL, cred_type TEXT NOT NULL,"
    " value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " created_at TEXT NOT NULL DEFAULT '', derived_from TEXT NOT NULL DEFAULT '',"
    " UNIQUE (repo_key, cred_type))"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.execute(_RC_DDL)
    raw.commit()
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def cfg(tmp_path):
    """最小配置目录：两个仓库平台 + 两个登录渠道（一个纯登录）。"""
    d = tmp_path / "configs"
    (d / "platforms").mkdir(parents=True)
    (d / "oauth-providers.json").write_text(json.dumps({
        "gitee": {"name": "Gitee", "scope": "user_info", "pkce_enabled": False},
        "huawei": {"name": "华为", "scope": "openid", "id_field": "openID"},
    }, ensure_ascii=False), encoding="utf-8")
    for p, host in (("gitee", "gitee.com"), ("gitlab", "gitlab.com")):
        (d / "platforms" / f"{p}.json").write_text(json.dumps({
            "name": p.title(), "dialect": "gitlab" if p == "gitlab" else "github", "host": host,
        }), encoding="utf-8")
    return str(d)


@pytest.fixture(autouse=True)
def fake_meta(monkeypatch):
    """meta 用内存 dict 代替（epoch / migrated / 锁）。"""
    store: dict[str, str] = {}

    def _get(k):
        return store.get(k, "")

    def _set(k, v):
        store[k] = v

    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "get_meta_value", _get, raising=False)
    monkeypatch.setattr(udb, "set_meta_value", _set, raising=False)
    return store


def test_first_import_then_idempotent(session, cfg, fake_meta):
    r1 = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert r1.changed >= 4  # 2 channels + 2 platforms
    assert r1.epoch_after == 1
    assert OAuthChannelRepo.get(session, "gitee") is not None

    # 第二次：全部 skipped，epoch 不变
    r2 = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert {c.action for c in r2.channels} == {"skipped"}
    assert r2.changed == 0 and r2.epoch_after == r2.epoch_before == 1


def test_content_change_updates_only_that_provider(session, cfg, fake_meta):
    importer.scan_and_apply(session, config_dir=cfg, apply=True)
    path = f"{cfg}/platforms/gitee.json"
    data = json.loads(open(path, encoding="utf-8").read())
    data["host"] = "gitee.org"
    open(path, "w", encoding="utf-8").write(json.dumps(data))
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    acts = {c.provider: c.action for c in r.platforms}
    assert acts["gitee"] == "updated" and acts["gitlab"] == "skipped"
    assert RepoPlatformModel.__tablename__ and importer.current_epoch() == 2


def test_file_removed_marks_orphaned_and_restore(session, cfg, fake_meta):
    importer.scan_and_apply(session, config_dir=cfg, apply=True)
    h = canonical_hash({"name": "Gitlab", "dialect": "gitlab", "host": "gitlab.com"})
    import os
    os.remove(f"{cfg}/platforms/gitlab.json")
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert "gitlab" in r.orphaned["platforms"]
    # 原样加回（同内容）→ restored（无视 hash）
    open(f"{cfg}/platforms/gitlab.json", "w", encoding="utf-8").write(
        json.dumps({"name": "Gitlab", "dialect": "gitlab", "host": "gitlab.com"}))
    r2 = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    acts = {c.provider: c.action for c in r2.platforms}
    assert acts["gitlab"] == "restored"


def test_manual_row_is_not_overwritten(session, cfg, fake_meta):
    importer.scan_and_apply(session, config_dir=cfg, apply=True)
    row = OAuthChannelRepo.get(session, "gitee")
    row.source = "manual"
    row.name = "管理面改名"
    session.flush()
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    acts = {c.provider: c.action for c in r.channels}
    assert acts["gitee"] == "manual_kept"
    assert OAuthChannelRepo.get(session, "gitee").name == "管理面改名"


def test_unknown_key_fails_only_that_provider(session, cfg, fake_meta):
    data = json.loads(open(f"{cfg}/platforms/gitee.json", encoding="utf-8").read())
    data["totally_unknown"] = 1
    open(f"{cfg}/platforms/gitee.json", "w", encoding="utf-8").write(json.dumps(data))
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert any("platform:gitee" in f for f in r.failed)
    # 其他平台照常导入
    assert RepoPlatformModel and OAuthChannelRepo.get(session, "huawei") is not None
    assert importer.scan_and_apply(session, config_dir=cfg, apply=False).failed  # preview 也报


def test_pure_login_channel_has_no_ghost_link(session, cfg, fake_meta):
    """纯登录渠道（huawei 无同名平台）在任何模式下都不产生 link 行。

    2026-11-14 口径：没有关系文件 → 按 provider 同名默认关联；文件存在 → 以文件为准
    （哪怕 links 为空）。huawei 两种模式下都无平台可关联，因此恒为 0。
    """
    import os as _os

    path = _os.path.join(cfg, "channel-repo-links.json")
    for mode in ("no-file", "explicit-empty"):
        if mode == "explicit-empty":
            open(path, "w", encoding="utf-8").write(json.dumps({"links": []}))
        elif _os.path.exists(path):
            _os.remove(path)
        r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
        assert r.links.get("huawei") == 0, mode


def test_missing_link_file_means_no_cross_pairs(session, cfg, fake_meta):
    """没有授权配置文件 → 没有任何跨名配对（同域由运行时隐式提供，不再派生同域行）。

    2026-11-14 定稿：同域配对属错误配置（渠道自己即平台），导入器不再生成 (c→c) 行。
    """
    import os

    path = os.path.join(cfg, "channel-repo-links.json")
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    try:
        r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
        assert r.links.get("gitee", 0) == 0, "缺文件时不得派生同域配对行"
        assert any("同域渠道天然可驱动本平台" in w for w in r.warnings)
        from src.kernel.persistence.repositories_platform_object import ChannelRepoLinkRepo
        assert list(ChannelRepoLinkRepo.list_by_oauth(session, "gitee")) == []
    finally:
        if existed:
            open(path, "w", encoding="utf-8").write(json.dumps({"links": []}))


def test_link_file_present_is_authoritative(session, cfg, fake_meta):
    """文件存在（哪怕 links 为空）→ 不回落同名默认，显式即真源。"""
    open(f"{cfg}/channel-repo-links.json", "w", encoding="utf-8").write(json.dumps({"links": []}))
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert r.links.get("gitee") == 0 and r.links.get("huawei") == 0
    assert not any("同名默认关联" in w for w in r.warnings)


def test_explicit_link_to_missing_platform_fails(session, cfg, fake_meta):
    """授权指向不存在的平台 = 配置错误（整渠道拒绝），且不写库。"""
    links = {"links": [{"oauth_provider": "huawei", "repo_provider": "codearts"}]}
    open(f"{cfg}/channel-repo-links.json", "w", encoding="utf-8").write(
        json.dumps(links, ensure_ascii=False))
    r = importer.scan_and_apply(session, config_dir=cfg, apply=True)
    assert any("codearts" in f for f in r.failed)
    assert "huawei" not in r.links




def test_preview_does_not_write(session, cfg, fake_meta):
    r = importer.scan_and_apply(session, config_dir=cfg, apply=False)
    assert r.applied is False and r.epoch_after == 0
    assert r.changed >= 4  # 预估会插入
    assert session.query(OAuthChannelModel).count() == 0


def test_startup_import_takes_lock_and_marks_migrated(session, cfg, fake_meta):
    r = importer.run_startup_import(session, owner="inst-1", config_dir=cfg)
    assert r.applied and fake_meta.get("config_state")
    assert '"migrated": true' in fake_meta["config_state"].replace("True", "true")
    assert fake_meta.get("config_import_lock") == ""
