"""D109 管理面接口契约（UC-35~39）：路径=领域对象名、无父容器、无整卡 PUT。

对应设计分片 d109 §19、需求 FR-30。函数级直调（admin={} 绕过依赖注入），
引擎用内存 sqlite + 平台对象 DDL。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL

ADMIN_DIR = Path(__file__).resolve().parents[2] / "src" / "domains" / "mgmt" / "admin"
# 2026-11-14 用户口径：不存在「平台对象」这个领域对象，管理面按聚合平铺
# （oauth_channels / repo_platforms / channel_repo_links / user_repo_platform_links /
#   bot_accounts + 派生视图），不设父容器目录。credential_caps 聚合随
# configs/repo-credentials.json 删除（2026-11-14）。
OBJ_MODULES = (
    "oauth_channel_routes.py",
    "repo_platform_routes.py",
    "channel_repo_link_routes.py",
    "user_repo_platform_link_routes.py",
    "views_routes.py",
)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", future=True)
    raw = eng.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    s = Session(eng)
    s.execute(text(
        "INSERT INTO grape_t_oauth_channels (id,provider,name,type,auth_url,token_url,user_url,scope,enabled) VALUES "
        "('c1','gitee','Gitee','oauth2','https://gitee.com/oauth/authorize','https://gitee.com/oauth/token','','user_info',1),"
        "('c2','github','GitHub','oauth2','','','','read:user',1)"
    ))
    s.execute(text(
        "INSERT INTO grape_t_repo_platforms (id,provider,name,dialect,host,api_base,enabled) VALUES "
        "('p1','gitee','Gitee','github','gitee.com','https://gitee.com/api/v5',1),"
        "('p2','github','GitHub','github','github.com','https://api.github.com',1)"
    ))
    s.execute(text(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source) VALUES "
        "('l1','gitee','gitee','derived')"
    ))
    s.commit()
    s.close()
    yield eng
    eng.dispose()


@pytest.fixture()
def wired(engine, monkeypatch):
    """把三个入口都指向内存引擎：ORM get_engine / uow / bump_epoch。"""
    from src.kernel.persistence import engine as engine_mod
    from src.kernel.persistence import uow as uow_mod
    from src.domains.platform import importer

    class _U:
        def __enter__(self):
            self.s = Session(engine)
            return self.s

        def __exit__(self, *a):
            self.s.commit()
            self.s.close()
            return False

    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(uow_mod, "uow", lambda: _U())
    monkeypatch.setattr(uow_mod, "get_engine", lambda: engine, raising=False)
    monkeypatch.setattr(importer, "bump_epoch", lambda: None)
    return engine


# ---------- UC-35：六聚合集合 GET（只回自己字段、凭据只回布尔）----------

def test_oauth_channels_collection(wired):
    from src.domains.mgmt.admin import oauth_channel_routes as m

    out = m.list_channels(admin={})
    assert out["source"] == "table" and len(out["channels"]) == 2
    item = {c["provider"]: c for c in out["channels"]}["gitee"]
    assert set(item["credential_present"]) == {"client_id", "client_secret"}
    assert "client_secret" not in str(item["credential_present"].values())
    assert isinstance(item["repo_links"], list) and item["capability"]["login"] is True


def test_repo_platforms_collection(wired):
    from src.domains.mgmt.admin import repo_platform_routes as m

    out = m.list_platforms(admin={})
    assert out["source"] == "table" and len(out["platforms"]) == 2
    gitee = {p["provider"]: p for p in out["platforms"]}["gitee"]
    assert "pat_create_url" in gitee, "平台卡应直出 PAT 创建页链接（原 caps 字段已并入平台行）"
    assert gitee["capability"]["enabled"] is True
    assert all("token" not in b for b in gitee["bot_accounts"])


def test_links_collection(wired):
    from src.domains.mgmt.admin import channel_repo_link_routes as links

    lk = links.list_links(admin={})
    assert lk["links"][0]["repo_provider"] == "gitee"


# ---------- UC-36：单条 GET 与 404 ----------

def test_single_get_and_404(wired):
    from src.domains.mgmt.admin import (
        oauth_channel_routes as ocr,
        repo_platform_routes as rpr,
    )

    assert ocr.get_channel("gitee", admin={})["name"] == "Gitee"
    for fn, ident in ((ocr.get_channel, "nope"), (rpr.get_platform, "nope")):
        with pytest.raises(HTTPException) as ei:
            fn(ident, admin={})
        assert ei.value.status_code == 404
        assert ei.value.detail["code"] == "NOT_FOUND"


# ---------- UC-37：channel-repo-links manual 写/删 + 目标校验 ----------

def test_manual_link_put_and_delete(wired):
    from src.domains.mgmt.admin import channel_repo_link_routes as m

    # 目标平台存在 → manual（配对即授权：请求体已无档位）
    out = m.put_link("gitee", "github", admin={})
    assert out["source"] == "manual"
    row = m.get_link("gitee", "github", admin={})
    assert row["source"] == "manual" and "relation" not in row
    # 删除 manual
    assert m.delete_link("gitee", "github", admin={})["ok"] is True
    # 目标不存在 → 422
    with pytest.raises(HTTPException) as ei:
        m.put_link("gitee", "codearts", admin={})
    assert ei.value.status_code == 422 and ei.value.detail["code"] == "TARGET_NOT_FOUND"
    # 删除 config/derived 行 → 409
    with pytest.raises(HTTPException) as ei2:
        m.delete_link("gitee", "gitee", admin={})
    assert ei2.value.status_code == 409 and ei2.value.detail["code"] == "NOT_MANUAL"


# ---------- UC-38：派生视图（非实体，路径不带对象前缀）----------

def test_views_capabilities_and_user_platforms(wired):
    from src.domains.mgmt.admin import views_routes as v

    caps = v.get_capabilities(admin={})
    assert caps["capabilities"] and "gitee" in caps["capabilities"]
    with pytest.raises(HTTPException) as ei:
        v.get_user_platforms(admin={})  # 缺 user_id → 422
    assert ei.value.status_code == 422


# ---------- UC-39：契约守卫（AST）----------

def _routes(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in ("get", "put", "post", "delete")
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    yield dec.func.attr, dec.args[0].value


def test_no_parent_container_and_no_whole_card_put():
    files = [ADMIN_DIR / name for name in OBJ_MODULES]
    bad_container, whole_card = [], []
    allowed_put_suffix = ("/credentials", "/enabled", "}", "{username}")
    for f in files:
        for method, path in _routes(f):
            if "platform-objects" in path:
                bad_container.append(f"{f.name}:{path}")
            if method != "put":
                continue
            # 整卡写 = PUT 直接落在集合 item（单段路径，且不是已知子资源）
            segs = [s for s in path.split("/") if s]
            if len(segs) == 1 and path.endswith("}") and not path.endswith(allowed_put_suffix):
                whole_card.append(f"{f.name}:{path}")
    assert not bad_container, f"出现虚构父容器 platform-objects: {bad_container}"
    assert not whole_card, f"出现整卡 PUT: {whole_card}"