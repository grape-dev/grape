"""Issue #345 方向3：token_error 公共 401 识别函数测试。"""
import pytest
from src.vcs.token_error import (
    BOT_TOKEN_INVALID_MSG,
    extract_http_status_from_exc,
    extract_http_status_from_result,
    is_auth_error_exc,
    is_auth_error_result,
)


class TestExtractHttpStatusFromExc:
    """从 RuntimeError 消息提取 HTTP status code。"""

    def test_gitcode_401(self):
        exc = RuntimeError("GitCode API GET /user failed (401): Unauthorized")
        assert extract_http_status_from_exc(exc) == 401

    def test_gitcode_403(self):
        exc = RuntimeError("GitCode API POST failed (403): Forbidden")
        assert extract_http_status_from_exc(exc) == 403

    def test_comment_failed(self):
        exc = RuntimeError("GitCode comment failed (401): token not found")
        assert extract_http_status_from_exc(exc) == 401

    def test_no_code(self):
        exc = RuntimeError("Cannot reach GitCode API: timeout")
        assert extract_http_status_from_exc(exc) is None

    def test_no_parentheses(self):
        exc = RuntimeError("something went wrong")
        assert extract_http_status_from_exc(exc) is None

    def test_non_3digit(self):
        exc = RuntimeError("failed (40): too short")
        assert extract_http_status_from_exc(exc) is None

    def test_first_code_only(self):
        """多组括号数字时取第一个（消息中 HTTP code 先出现）。"""
        exc = RuntimeError("failed (401): error code 1401 detected")
        assert extract_http_status_from_exc(exc) == 401


class TestExtractHttpStatusFromResult:
    """从 VcsApiClient 返回的 dict 提取 HTTP status code。"""

    def test_401(self):
        result = {"error": "HTTP 401", "detail": "Unauthorized"}
        assert extract_http_status_from_result(result) == 401

    def test_403(self):
        result = {"error": "HTTP 403", "detail": "Forbidden"}
        assert extract_http_status_from_result(result) == 403

    def test_no_error_key(self):
        result = {"number": 123, "title": "test"}
        assert extract_http_status_from_result(result) is None

    def test_non_http_error(self):
        result = {"error": "Connection timeout"}
        assert extract_http_status_from_result(result) is None

    def test_not_dict(self):
        assert extract_http_status_from_result("not a dict") is None
        assert extract_http_status_from_result(None) is None


class TestIsAuthErrorExc:
    """判断异常是否为 401 认证失败。"""

    def test_401_is_auth_error(self):
        exc = RuntimeError("failed (401): bad token")
        assert is_auth_error_exc(exc) is True

    def test_403_not_auth_error(self):
        exc = RuntimeError("failed (403): forbidden")
        assert is_auth_error_exc(exc) is False

    def test_500_not_auth_error(self):
        exc = RuntimeError("failed (500): server error")
        assert is_auth_error_exc(exc) is False

    def test_no_code_not_auth_error(self):
        exc = RuntimeError("timeout")
        assert is_auth_error_exc(exc) is False


class TestIsAuthErrorResponse:
    """判断 VcsApiClient 返回值是否为 401。"""

    def test_401_is_auth_error(self):
        result = {"error": "HTTP 401", "detail": "bad token"}
        assert is_auth_error_result(result) is True

    def test_403_not_auth_error(self):
        result = {"error": "HTTP 403", "detail": "forbidden"}
        assert is_auth_error_result(result) is False

    def test_success_not_auth_error(self):
        result = {"number": 42, "title": "test"}
        assert is_auth_error_result(result) is False

    def test_non_dict_not_auth_error(self):
        assert is_auth_error_result(None) is False
        assert is_auth_error_result("string") is False

    def test_dict_without_error_key(self):
        assert is_auth_error_result({"data": []}) is False


class TestBotTokenInvalidMsg:
    """提示文案常量。"""

    def test_constant_value(self):
        assert "Bot Token" in BOT_TOKEN_INVALID_MSG
        assert "失效" in BOT_TOKEN_INVALID_MSG
        assert "检查" in BOT_TOKEN_INVALID_MSG
