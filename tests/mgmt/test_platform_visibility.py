"""用户面平台可见性（方案甲：真源 = 平台行 enabled）与 webhook 基址语义。

2026-11-15 用户拍板把可见性收敛到**一个开关**（`grape_t_repo_platforms.enabled`），
`meta.vcs_platforms_hidden` 退役（迁移 202611170021）。这里锁五件事：

1. 判据：`is_platform_visible()` = 平台行 enabled（无行/未迁移 → 可见，fail-open）。
2. 用户面：`?provider=<disabled>` → 403 `PLATFORM_DISABLED`，且判定必须排在
   "无绑定 → 200 `_no_vcs_binding`"之前（线上实跑抓到过顺序 bug）。
3. 合并列表不发起 disabled 平台拉取（严格语义：不提供 = 谁都看不到，避免
   "看得到但配不了凭据"的半可用态）。
4. **registry 哨兵**：表里有行但全部 disabled → 返回空目录 `[]`，不得回退平台卡
   （旧实现把"全禁用"当成"表不可用"，导致"全关 = 全开"）。
5. 关闭平台的**影响面预检**：无 `confirm=1` 且影响面 > 0 → 409 并回统计。

另含 webhook_url 口径锁（只写基址，平台路径来自平台卡）——行为未变，锁住语义。
"""
import sqlite3
from types import SimpleNamespace

import pytest

import src.domains.platform.visibility as vis


# ---------- 1. 判据 ----------

@pytest.fixture
def table_backed(monkeypatch):
    import src.domains.platform.importer as imp
    monkeypatch.setattr(imp, "is_table_backed", lambda: True)
    yield monkeypatch


def _fake_row(provider, enabled):
    return SimpleNamespace(provider=provider, enabled=enabled)


def test_visibility_follows_platform_enabled(table_backed):
    import src.kernel.persistence.engine as eng
    import src.kernel.persistence.repositories_platform_object as repos

    rows = {"gitcode": _fake_row("gitcode", 1), "gitlab": _fake_row("gitlab", 0)}
    table_backed.setattr(repos.RepoPlatformRepo, "get",
                         staticmethod(lambda s, p: rows.get(p)))
    table_backed.setattr(eng, "get_engine", lambda: sqlite3.connect(":memory:"))

    assert vis.is_platform_visible("gitcode") is True
    assert vis.is_platform_visible("gitlab") is False
    # 表里没有行 = 纯配置平台（尚未导入）→ 可见，避免"导入前一律不可用"
    assert vis.is_platform_visible("gitee") is True


def test_visibility_fails_open_when_table_not_ready(monkeypatch):
    import src.domains.platform.importer as imp
    monkeypatch.setattr(imp, "is_table_backed", lambda: False)
    assert vis.is_platform_visible("gitlab") is True


# ---------- 2. 用户面 403 ----------

@pytest.fixture
def gitlab_disabled(monkeypatch):
    import src.domains.mgmt.routes.repos as R

    monkeypatch.setattr(vis, "is_platform_visible", lambda p: str(p) != "gitlab")
    monkeypatch.setattr(R, "is_platform_visible", lambda p: str(p) != "gitlab", raising=False)
    yield monkeypatch


def test_explicit_request_for_disabled_platform_is_403(gitlab_disabled, monkeypatch):
    from fastapi import HTTPException

    import src.domains.mgmt.auth.grape_account_db as gadb
    from src.domains.mgmt.routes.repos import list_member_repos

    monkeypatch.setattr(gadb, "list_platform_links", lambda uid: [{"provider": "gitlab"}])
    with pytest.raises(HTTPException) as ei:
        list_member_repos(user={"id": "1", "username": "u", "org_id": 0}, provider="gitlab")
    assert ei.value.status_code == 403
    detail = ei.value.detail
    assert isinstance(detail, dict) and detail.get("error_code") == "PLATFORM_DISABLED"


def test_disabled_platform_is_403_even_without_binding(gitlab_disabled, monkeypatch):
    """顺序纪律：判定先于"未绑定 → 200"早退，否则"已禁用"被解释成"你没绑"。"""
    from fastapi import HTTPException

    import src.domains.mgmt.auth.grape_account_db as gadb
    from src.domains.mgmt.routes.repos import list_member_repos

    monkeypatch.setattr(gadb, "list_platform_links", lambda uid: [])
    with pytest.raises(HTTPException) as ei:
        list_member_repos(user={"id": "1", "username": "u", "org_id": 0}, provider="gitlab")
    assert ei.value.status_code == 403
    assert isinstance(ei.value.detail, dict) and ei.value.detail.get("error_code") == "PLATFORM_DISABLED"


# ---------- 3. 合并列表不拉 disabled ----------

def test_merged_list_does_not_fetch_disabled_platform(gitlab_disabled, monkeypatch):
    import src.domains.mgmt.auth.grape_account_db as gadb
    import src.domains.mgmt.routes.repos as R
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(gadb, "list_platform_links", lambda uid: [{"provider": "gitlab"}])
    monkeypatch.setattr(udb, "get_user_repos", lambda uid: [])
    monkeypatch.setattr(udb, "get_user_repos_synced_at", lambda uid: None)
    monkeypatch.setattr(R, "with_oauth_token",
                        lambda *a, **kw: pytest.fail("disabled 平台不应触发 VCS 拉取"))

    out = R.list_member_repos(user={"id": "1", "username": "u", "org_id": 0}, provider="")
    assert out["repos"] == []


# ---------- 4. registry 哨兵：全禁用 ≠ 回退卡片 ----------

def test_registry_returns_empty_when_every_platform_is_disabled(monkeypatch):
    import src.domains.platform.registry as R

    monkeypatch.setattr(R, "_descriptors_from_tables", lambda: [])
    monkeypatch.setattr(R, "_descriptors_from_cards",
                        lambda: pytest.fail("全部 disabled 时不得回退平台卡（全关变全开）"))
    assert R.platform_descriptors() == []


def test_registry_falls_back_to_cards_only_when_table_is_empty(monkeypatch):
    import src.domains.platform.registry as R

    monkeypatch.setattr(R, "_descriptors_from_tables", lambda: None)
    monkeypatch.setattr(R, "_descriptors_from_cards", lambda: [{"provider": "gitcode"}])
    assert [d["provider"] for d in R.platform_descriptors()] == ["gitcode"]


def test_registry_filters_disabled_rows_from_real_table(monkeypatch):
    """真表路径：行存在但 enabled=0 → 该平台不进目录，且结果不是 None。"""
    from sqlalchemy import create_engine

    import src.domains.platform.importer as imp
    import src.domains.platform.registry as R
    import src.kernel.persistence.engine as eng

    engine = create_engine("sqlite://")
    # 用真实模型建表：手写 DDL 会漏列（ORM SELECT 是全列清单），测出来的错不是业务错
    from src.kernel.persistence.models_platform_object import RepoPlatformModel
    RepoPlatformModel.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(RepoPlatformModel.__table__.insert(), [
            {"id": "1", "provider": "gitcode", "name": "GitCode", "host": "gitcode.com",
             "dialect": "github", "enabled": 0},
            {"id": "2", "provider": "gitee", "name": "Gitee", "host": "gitee.com",
             "dialect": "github", "enabled": 1},
        ])
    monkeypatch.setattr(imp, "is_table_backed", lambda: True)
    monkeypatch.setattr(eng, "get_engine", lambda: engine)

    out = R._descriptors_from_tables()
    assert out is not None, "有行时不得返回 None（否则会回退平台卡）"
    assert [d["provider"] for d in out] == ["gitee"]


# ---------- 5. 影响面预检 ----------

def test_disable_requires_confirm_when_impact(monkeypatch):
    from fastapi import HTTPException

    from src.domains.mgmt.admin import repo_platform_routes as A

    monkeypatch.setattr(A, "_provider_impact",
                        lambda p: {"provider": p, "repos": 7, "users": 2,
                                   "managed_repos": 1, "pat_users": 1})
    with pytest.raises(HTTPException) as ei:
        A.set_enabled("gitlab", A.EnabledBody(enabled=False), confirm=0, admin={"username": "a"})
    assert ei.value.status_code == 409
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("error_code") == "PLATFORM_DISABLE_NEEDS_CONFIRM"
    assert detail["impact"]["repos"] == 7


def test_disable_proceeds_with_confirm(monkeypatch):
    import src.kernel.persistence.repositories_platform_object as repos
    from src.domains.mgmt.admin import repo_platform_routes as A

    monkeypatch.setattr(A, "_provider_impact",
                        lambda p: {"provider": p, "repos": 7, "users": 2,
                                   "managed_repos": 1, "pat_users": 1})
    monkeypatch.setattr(repos.RepoPlatformRepo, "set_enabled", staticmethod(lambda s, p, e: True))
    monkeypatch.setattr(A, "uow_write", lambda fn: fn(None))
    out = A.set_enabled("gitlab", A.EnabledBody(enabled=False), confirm=1, admin={"username": "a"})
    assert out["ok"] is True and out["impact"]["users"] == 2


def test_enable_needs_no_confirm(monkeypatch):
    import src.kernel.persistence.repositories_platform_object as repos
    from src.domains.mgmt.admin import repo_platform_routes as A

    monkeypatch.setattr(repos.RepoPlatformRepo, "set_enabled", staticmethod(lambda s, p, e: True))
    monkeypatch.setattr(A, "uow_write", lambda fn: fn(None))
    out = A.set_enabled("gitlab", A.EnabledBody(enabled=True), confirm=0, admin={"username": "a"})
    assert out["ok"] is True and out["impact"] is None


# ---------- 6. webhook 基址语义（行为未变，锁口径） ----------

def test_webhook_base_is_host_only_and_platform_path_comes_from_card():
    from src.domains.mgmt.user.db import _webhook_url_for_provider

    assert _webhook_url_for_provider("https://grape.example.com/webhook/gitlab", "gitcode") \
        == "https://grape.example.com/webhook/gitcode"
    assert _webhook_url_for_provider("https://grape.example.com", "gitlab") \
        == "https://grape.example.com/webhook/gitlab"


def test_get_webhook_url_returns_configured_base_unchanged(monkeypatch):
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "get_meta_value",
                        lambda key: "https://grape.example.com" if key == "webhook_url" else "")
    monkeypatch.setattr("src.kernel.config.read_server_config", lambda: {})
    assert udb.get_webhook_url() == "https://grape.example.com"

# ---------- 7. 默认不展示：写死在初始化代码 ----------

def test_default_hidden_platforms_listed_in_init_code():
    """默认不展示的平台写在初始化代码的常量里（用户裁定：不进平台卡）。"""
    from src.kernel.persistence.repositories_platform_object import (
        DEFAULT_HIDDEN_PLATFORMS, _default_enabled)
    assert DEFAULT_HIDDEN_PLATFORMS == frozenset({"gitlab"})
    assert _default_enabled("gitlab") == 0
    assert _default_enabled("gitcode") == 1


def test_new_platform_row_is_seeded_with_default_visibility(monkeypatch):
    """新建平台行：gitlab 落 enabled=0，其余落 1；之后导入不再覆盖（DB 权威）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.kernel.persistence.models_platform_object import RepoPlatformModel
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    engine = create_engine("sqlite://")
    RepoPlatformModel.__table__.create(engine)
    with Session(engine) as s:
        RepoPlatformRepo.upsert_from_config(s, "gitlab", {"name": "GitLab", "host": "gitlab.com"}, "h1")
        RepoPlatformRepo.upsert_from_config(s, "gitee", {"name": "Gitee", "host": "gitee.com"}, "h2")
        s.commit()
        assert RepoPlatformRepo.get(s, "gitlab").enabled == 0
        assert RepoPlatformRepo.get(s, "gitee").enabled == 1

        # 运维在管理面打开 gitlab 后，再导入（hash 变化）不得把它翻回去
        RepoPlatformRepo.set_enabled(s, "gitlab", True)
        s.commit()
        RepoPlatformRepo.upsert_from_config(s, "gitlab",
                                           {"name": "GitLab", "host": "gitlab.com",
                                            "api_base": "https://gitlab.com/api/v4"}, "h1-changed")
        s.commit()
        assert RepoPlatformRepo.get(s, "gitlab").enabled == 1, "导入覆盖了管理面的开关"


def test_migration_021_applies_initial_default_and_retires_meta_list(monkeypatch):
    """迁移 021：把 meta 名单搬成 enabled=0，并把默认不展示平台补成 0，最后删除 meta 键。"""
    import json as _json

    import src.kernel.session_manager as sm
    from src.api import db_setup

    class _NoopLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE grape_t_repo_platforms (provider TEXT, enabled INTEGER, updated_at TEXT)")
    conn.execute("INSERT INTO meta VALUES ('vcs_platforms_hidden', ?)", (_json.dumps(["github"]),))
    for prov in ("gitcode", "github", "gitee", "gitlab"):
        conn.execute("INSERT INTO grape_t_repo_platforms VALUES (?,1,'')", (prov,))
    conn.commit()
    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "db_lock", _NoopLock(), raising=False)

    db_setup._fn_retire_vcs_platforms_hidden()

    rows = dict(conn.execute("SELECT provider, enabled FROM grape_t_repo_platforms"))
    assert rows["github"] == 0, "meta 名单里的平台没有落成 enabled=0"
    assert rows["gitlab"] == 0, "初始化默认不展示的平台没有落成 enabled=0"
    assert rows["gitcode"] == 1 and rows["gitee"] == 1
    assert conn.execute("SELECT value FROM meta WHERE key='vcs_platforms_hidden'").fetchone() is None
    db_setup._fn_retire_vcs_platforms_hidden()  # 幂等
    assert dict(conn.execute("SELECT provider, enabled FROM grape_t_repo_platforms")) == rows


def test_disable_requires_confirm_even_with_zero_impact(monkeypatch):
    """关闭一律要确认（含影响面为 0 的平台）。

    2026-11-15 实锤：早先按"影响面 > 0 才拦"，导致 gitlab 这种没有仓库的平台
    开关可以随便点（其他平台却被 409 拦住），操作者无法预判行为。
    """
    from fastapi import HTTPException

    from src.domains.mgmt.admin import repo_platform_routes as A

    monkeypatch.setattr(A, "_provider_impact",
                        lambda p: {"provider": p, "repos": 0, "users": 0,
                                   "managed_repos": 0, "pat_users": 0})
    with pytest.raises(HTTPException) as ei:
        A.set_enabled("gitlab", A.EnabledBody(enabled=False), confirm=0, admin={"username": "a"})
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)
    assert ei.value.detail.get("error_code") == "PLATFORM_DISABLE_NEEDS_CONFIRM"
    assert ei.value.detail["impact"]["repos"] == 0


def test_admin_page_shows_confirm_dialog_before_retrying(monkeypatch):
    """前端必须弹窗展示影响面再带 confirm=1 重发，否则开关永远 409（页面写错的实锤）。"""
    from pathlib import Path

    vue = Path(__file__).resolve().parents[2] / "src/api/frontend/app/src/views/AdminPlatforms.vue"
    text = vue.read_text(encoding="utf-8")
    assert "PLATFORM_DISABLE_NEEDS_CONFIRM" in text, "前端没有识别 409 预检错误码"
    assert "confirm=1" in text, "前端确认后没有带 confirm=1 重发"
    assert "impact" in text, "弹窗没有展示影响面统计"
    # 用户要求：弹层必须是页面内 Vue 模板（不用原生 window.confirm）；样式必须自带，
    # 否则 admin 页没加载设计令牌时 position:fixed 失效、弹层会"掉到页面外面"
    assert "Teleport" in text and "ap-confirm-mask" in text, "确认层不是页面内 Vue 弹层"
    assert "position: fixed" in text and "inset: 0" in text, "弹层遮罩没有自带定位样式"
    assert "window.confirm(" not in text, "仍在用原生 window.confirm（用户已明确不要）"
