"""Issue #683: 422 校验错误响应携带字段级详情（字段名 + 合法值枚举）。

覆盖：
- GET /api/issues/{owner}/{repo} 非法 status（all/open）→ 422 {code, message}，
  message 含 'status' 与全部合法值枚举
- 其他 pattern 参数（sort_order）同样携带合法值枚举
- 合法 status 值（processing 等）行为不回归
- extract_allowed_values / validation_error_response 单元级行为
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db

client = TestClient(app)

OWNER = "test_issues_683"
REPO = "hello"
ALLOWED = ["active", "processing", "awaiting_input", "pending_review", "completed", "cancelled"]

_headers = {}


def setup_module():
    global _headers
    uid = grape_account_db.create_grape_account("test_issues_683", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_issues_683")
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    token = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    _headers["Authorization"] = f"Bearer {token}"


def test_422_invalid_status_all():
    resp = client.get(f"/api/issues/{OWNER}/{REPO}", params={"status": "all"}, headers=_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    msg = body["message"]
    assert "status" in msg
    for v in ALLOWED:
        assert v in msg, f"allowed value {v} missing in message: {msg}"


def test_422_invalid_status_open():
    resp = client.get(f"/api/issues/{OWNER}/{REPO}", params={"status": "open"}, headers=_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    assert "'status'" in body["message"]
    assert "allowed values are" in body["message"]


def test_422_invalid_sort_order():
    resp = client.get(f"/api/issues/{OWNER}/{REPO}", params={"sort_order": "up"}, headers=_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    assert "'sort_order'" in body["message"]
    assert "desc" in body["message"] and "asc" in body["message"]


def test_valid_status_no_regression():
    for v in ALLOWED:
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", params={"status": v}, headers=_headers)
        assert resp.status_code == 200, f"valid status {v} regressed: {resp.status_code}"
        assert "issues" in resp.json()


def test_extract_allowed_values():
    from src.api.errors import extract_allowed_values
    assert extract_allowed_values("^(active|processing|)$") == ["active", "processing"]
    assert extract_allowed_values("^(desc|asc)$") == ["desc", "asc"]
    assert extract_allowed_values("") == []
    assert extract_allowed_values("weird") == ["weird"]


def test_validation_error_response_fallback():
    from src.api.errors import validation_error_response

    class FakeExc:
        def errors(self):
            return [{"loc": ("query", "page"), "msg": "Input should be greater than 0"}]

    resp = validation_error_response(FakeExc())
    assert resp.status_code == 422
    import json as _json
    body = _json.loads(resp.body)
    assert body["code"] == 422
    assert "'page'" in body["message"]
