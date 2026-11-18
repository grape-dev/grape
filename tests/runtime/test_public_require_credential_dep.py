"""runtime.public.require_credential 契约测试（PR #1699 审查 C1 回归）。

钉死两件容易回归的事：
1. **形参必须是 `Request` 注解**：本模块 `from __future__ import annotations` 下注解是
   字符串，FastAPI 靠模块全局名解析；缺注解（`def require_credential(request)`）时
   FastAPI 把 `request` 当成必填 **query 参数**，端点对任何请求先返回 422，凭据校验
   根本跑不到 —— insight 的 POST /api/obs/logs/ingest 全量 422 即此故障形态。
2. **作为 `Depends(...)` 的行为**：无凭据 / 错凭据 → 401（不是 422）；有效 `rta_`
   凭据 → 返回 `runtime_credentials.id`（int），供调用方落 source 名。

全部走 sqlite 测试库（tests/conftest.py 初始化）与内存 mini-app，不连生产库。
"""
import hashlib
import os
import sys
import typing

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime import public as rt  # noqa: E402

MARK = "pr1699cred"
TOKEN = "rta_" + MARK + "token123456"


def _clean():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE ?", (MARK + "%",))
        except Exception:
            pass
        db.commit()


def _mk_cred(name, token, device="dev-pr1699"):
    """插入一条 active 的 worker 凭据；返回凭据 id。"""
    from src.kernel.session_manager import db, db_lock
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status)"
                  " VALUES (?,?,?,'global','active')", (h, MARK + name, device))
        db.commit()
        c.execute("SELECT id FROM runtime_credentials WHERE token_hash=?", (h,))
        return int(c.fetchone()[0])


def _app():
    """只用 public 入口做依赖的 mini-app：与 insight 端点的用法一致。"""
    app = FastAPI()

    @app.post("/probe")
    def probe(credential_id: int = Depends(rt.require_credential)):
        return {"credential_id": credential_id}

    return app


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


def test_annotation_is_fastapi_request():
    """形参注解必须解析成 fastapi.Request（缺注解 = FastAPI 按 query 解析）。"""
    hints = typing.get_type_hints(rt.require_credential)
    assert hints.get("request") is Request, f"request 注解解析为 {hints.get('request')!r}"


def test_dependency_registers_no_query_parameter():
    """依赖不得在 OpenAPI 里登记任何必填 query 参数（缺注解时会出现 request=query）。"""
    op = _app().openapi()["paths"]["/probe"]["post"]
    params = op.get("parameters") or []
    query_names = [p.get("name") for p in params if p.get("in") == "query"]
    assert not query_names, f"public 依赖被当成 query 参数解析：{params}"


def test_missing_credential_returns_401_not_422():
    """无 Authorization → 401（凭据校验真正执行），不是 422（参数缺失）。"""
    with TestClient(_app()) as cli:
        r = cli.post("/probe")
    assert r.status_code == 401, r.text


def test_invalid_token_returns_401():
    with TestClient(_app()) as cli:
        r = cli.post("/probe", headers={"Authorization": "Bearer rta_notexist" + MARK})
    assert r.status_code == 401, r.text


def test_valid_credential_returns_credential_id():
    """有效 rta_ 凭据 → 依赖返回 runtime_credentials.id（int），端点可正常使用。"""
    cred_id = _mk_cred("w1", TOKEN)
    with TestClient(_app()) as cli:
        r = cli.post("/probe", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json()["credential_id"] == cred_id
