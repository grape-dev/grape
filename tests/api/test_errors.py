"""Grape 错误码统一契约测试（docs/weekly-03/error-code-standard-design.md §六）。

覆盖：
- errors.api_error：构造 HTTPException(detail={"code","message"})；内嵌 code 英文 message；internal 可选
- errors.business_ok_false：CLI 业务失败返回体 {"ok":False,"code","message"}
- 常量值
"""
import pytest
from fastapi import HTTPException

from src.api import errors


class TestApiError:
    def test_returns_http_exception(self):
        exc = errors.api_error(404, "Not Found")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 404

    def test_detail_code_and_message_english(self):
        exc = errors.api_error(400, "Invalid parameter")
        d = exc.detail
        assert d["code"] == 400
        assert d["message"] == "Invalid parameter"
        assert isinstance(d["message"], str)
        # message 应用 ASCII（英文，Agent 可读）
        assert d["message"].isascii()

    def test_internal_optional(self):
        exc = errors.api_error(502, "Bad Gateway", internal="upstream_gitcode_500")
        assert exc.detail["internal"] == "upstream_gitcode_500"

    def test_no_internal_by_default(self):
        exc = errors.api_error(500, "Internal Server Error")
        assert "internal" not in exc.detail

    def test_common_usage(self):
        exc = errors.api_error(errors.FORBIDDEN, "Forbidden")
        assert exc.status_code == 403
        assert exc.detail == {"code": 403, "message": "Forbidden"}


class TestBusinessOkFalse:
    def test_structure(self):
        r = errors.business_ok_false(400, "Assignee not registered")
        assert r == {"ok": False, "code": 400, "message": "Assignee not registered"}

    def test_message_english(self):
        r = errors.business_ok_false(502, "GitCode create issue failed")
        assert r["message"].isascii() and "ok" in r and r["ok"] is False


class TestConstants:
    def test_values(self):
        assert errors.OK == 200
        assert errors.CREATED == 201
        assert errors.NO_CONTENT == 204
        assert errors.BAD_REQUEST == 400
        assert errors.UNAUTHORIZED == 401
        assert errors.FORBIDDEN == 403
        assert errors.NOT_FOUND == 404
        assert errors.CONFLICT == 409
        assert errors.GONE == 410
        assert errors.TOO_MANY_REQUESTS == 429
        assert errors.INTERNAL_SERVER_ERROR == 500
        assert errors.BAD_GATEWAY == 502
        assert errors.SERVICE_UNAVAILABLE == 503