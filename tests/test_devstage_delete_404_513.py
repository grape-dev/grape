"""Tests for Issue #513 / #413: DELETE /api/devstage/environments/{不存在} 应返回 404 统一包络。

缺陷链（修复前）：
- 路由 delete_environment() 对 _delete_env() 抛出的 DevStageError 一律映射 502，
  原样回传上游华为云错误码（实测 HD.97420111 parameter invalid）与内部文案；
- 客户端 _request() 对 error_code != "0000" 不区分「资源不存在」与「上游真实故障」；
- 前置 _list_envs()（查环境名用于凭据清理）失败被吞（env_name=""），「不存在」信号丢失。

期望行为：
- 删除不存在的环境 → 404 统一包络 {"code":404,"message":...}，不含 HD.* 上游码；
- 上游真实故障（网络/白名单外错误码）→ 维持 502 透传（现有语义不回退）。

seam（公开接口）：
- 路由层：ds_route.delete_environment(devstage_id, user)（同步函数直接调用）；
- 客户端层：ds_client._request()（mock urlopen）。

注：同目录 test_devstage_filter.py 的 6 个 list 过滤用例在基线即红
（历史提交把 list_environments 从 async def 改成 def，测试仍用 asyncio.run 调用），
与本次改动无关；本文件用例均为同步调用，不受影响。
"""
import json
from unittest.mock import patch

import pytest

from src.api.routes import devstage as ds_route
from src.im import devstage_client as ds_client
from src.im.devstage_client import DevStageError

MISSING_ID = "does-not-exist-xyz"
# 删除不存在的 DevStage 环境时上游实测返回的错误码（parameter invalid）
UPSTREAM_NOT_FOUND_CODE = "HD.97420111"


def _make_user():
    return {"username": "test_user", "id": 1}


def _mock_ak_sk():
    return ("AKTEST", "SKTEST")


# ====== 原子用例（pytest -m atomic）======

@pytest.mark.atomic
@patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
@patch.object(ds_route, "_delete_env")
@patch.object(ds_route, "_list_envs")
def test_delete_missing_environment_is_404(mock_list, mock_delete, mock_aksk):
    """原子用例（Issue #513 / #413 同根因）：删除不存在的环境 → 404 统一包络。

    前置列表可枚举且无该 id → 直接 404 {"code":404,"message":...}，
    不发起上游删除调用，包络不含 HD.* 上游码。
    """
    from fastapi import HTTPException
    mock_list.return_value = [
        {"id": "env-1", "name": "env-one", "status": "cde.0004"},
    ]
    with pytest.raises(HTTPException) as exc_info:
        ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["code"] == 404
    assert "message" in detail and detail["message"]
    # 统一包络不泄露上游错误码/文案
    assert "HD." not in json.dumps(detail)
    # 前置判别生效：不发起注定失败的删除调用
    mock_delete.assert_not_called()


# ====== 路由层：404 分类 ======

class TestDeleteEnvironmentNotFound:
    """Route-level: delete of a missing environment must map to 404 unified envelope."""

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_missing_id_in_list_maps_404(self, mock_list, mock_delete, mock_aksk):
        """列表枚举成功但 id 不在列表 → 404 统一包络；不得发起上游删除调用。"""
        from fastapi import HTTPException
        mock_list.return_value = [
            {"id": "env-1", "name": "env-one", "status": "cde.0004"},
        ]
        with pytest.raises(HTTPException) as exc_info:
            ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
        assert exc_info.value.status_code == 404
        detail = exc_info.value.detail
        assert detail["code"] == 404
        assert "message" in detail and detail["message"]
        # 统一包络不泄露上游错误码/文案
        assert "HD." not in json.dumps(detail)
        # 前置判别生效：不发起注定失败的删除调用
        mock_delete.assert_not_called()

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_empty_list_maps_404(self, mock_list, mock_delete, mock_aksk):
        """列表为空（无任何环境）→ 删除任何 id 均 404。"""
        from fastapi import HTTPException
        mock_list.return_value = []
        with pytest.raises(HTTPException) as exc_info:
            ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == 404
        mock_delete.assert_not_called()

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_upstream_not_found_code_maps_404(self, mock_list, mock_delete, mock_aksk):
        """前置列表不可用（被吞路径）+ 删除调用返回上游 not_found 错误码 → 404 统一包络。"""
        from fastapi import HTTPException
        # 前置 _list_envs 失败（信号丢失场景：路由不因此 502，继续走删除调用分类）
        mock_list.side_effect = DevStageError("NETWORK", "Network error: boom")
        mock_delete.side_effect = DevStageError(
            UPSTREAM_NOT_FOUND_CODE, "parameter invalid: devstage_id not found",
            not_found=True)
        with pytest.raises(HTTPException) as exc_info:
            ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
        assert exc_info.value.status_code == 404
        detail = exc_info.value.detail
        assert detail["code"] == 404
        assert "HD." not in json.dumps(detail)
        mock_delete.assert_called_once()


# ====== 路由层：502 语义不回退 ======

class TestDeleteEnvironmentUpstreamFailure:
    """Route-level: real upstream failures must keep 502 semantics."""

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_upstream_real_failure_keeps_502(self, mock_list, mock_delete, mock_aksk):
        """上游真实故障（列表+删除均网络故障）→ 维持 502 透传上游码。"""
        from fastapi import HTTPException
        mock_list.side_effect = DevStageError("NETWORK", "Network error: boom")
        mock_delete.side_effect = DevStageError("NETWORK", "Network error: boom")
        with pytest.raises(HTTPException) as exc_info:
            ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "NETWORK"

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_upstream_other_error_code_keeps_502(self, mock_list, mock_delete, mock_aksk):
        """白名单外的上游错误码（如配额/鉴权失败）→ 仍 502 透传。"""
        from fastapi import HTTPException
        mock_list.side_effect = DevStageError("NETWORK", "Network error: boom")
        mock_delete.side_effect = DevStageError("HD.974xxxxx", "quota exceeded")
        with pytest.raises(HTTPException) as exc_info:
            ds_route.delete_environment(devstage_id=MISSING_ID, user=_make_user())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "HD.974xxxxx"

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_delete_env")
    @patch.object(ds_route, "_list_envs")
    def test_existing_environment_delete_unaffected(self, mock_list, mock_delete, mock_aksk):
        """存在的环境删除：行为不变（200 返回结果，前置列表成功取到环境名）。"""
        mock_list.return_value = [
            {"id": "env-1", "name": "env-one", "status": "cde.0004"},
        ]
        mock_delete.return_value = {"result": "ok"}
        result = ds_route.delete_environment(devstage_id="env-1", user=_make_user())
        assert result == {"result": "ok"}
        mock_delete.assert_called_once()


# ====== 客户端层：_request 错误分类 ======

class TestClientRequestClassification:
    """Client-level: _request must classify not-found vs real upstream failure."""

    def _patch_urlopen(self, raw_response):
        import urllib.request
        import io

        class _FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return self._payload.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return patch.object(
            urllib.request, "urlopen",
            return_value=_FakeResp(json.dumps(raw_response)))

    def test_upstream_not_found_error_code_flagged(self):
        """error_code=HD.97420111 → DevStageError.not_found=True（parameter invalid 场景）。"""
        with self._patch_urlopen({"error_code": UPSTREAM_NOT_FOUND_CODE,
                                  "error_msg": "parameter invalid"}):
            with pytest.raises(DevStageError) as exc_info:
                ds_client._request("DELETE", "/open-api-public/v1/devenvs/xxx", "AK", "SK")
        assert exc_info.value.not_found is True
        assert exc_info.value.code == UPSTREAM_NOT_FOUND_CODE

    def test_other_error_code_not_flagged(self):
        """白名单外错误码 → not_found=False（真实上游故障，维持 502 语义）。"""
        with self._patch_urlopen({"error_code": "HD.974xxxxx",
                                  "error_msg": "quota exceeded"}):
            with pytest.raises(DevStageError) as exc_info:
                ds_client._request("DELETE", "/open-api-public/v1/devenvs/xxx", "AK", "SK")
        assert exc_info.value.not_found is False

    def test_success_no_flag(self):
        """error_code=0000（成功）→ 正常返回 result，不抛异常。"""
        with self._patch_urlopen({"error_code": "0000", "result": {"ok": True}}):
            r = ds_client._request("DELETE", "/open-api-public/v1/devenvs/xxx", "AK", "SK")
        assert r == {"ok": True}

    def test_network_error_not_flagged(self):
        """网络层错误（URLError）→ not_found=False（真实故障）。"""
        import urllib.error
        import urllib.request
        with patch.object(urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("conn refused")):
            with pytest.raises(DevStageError) as exc_info:
                ds_client._request("DELETE", "/open-api-public/v1/devenvs/xxx", "AK", "SK")
        assert exc_info.value.not_found is False
        assert exc_info.value.code == "NETWORK"

    def test_devstage_error_default_not_found_false(self):
        """DevStageError(code, msg) 两参构造（存量调用）→ not_found 默认 False，向后兼容。"""
        e = DevStageError("HD.97420111", "parameter invalid")
        assert e.not_found is False

    def test_error_code_variants_not_flagged(self):
        """HTTP 层错误（HTTPError 带非 JSON body）→ not_found=False。"""
        import email.message
        import urllib.error
        import urllib.request
        err = urllib.error.HTTPError(
            url="https://devstation.myhuaweicloud.com/x", code=500,
            msg="Internal Server Error", hdrs=email.message.Message(), fp=None)
        with patch.object(urllib.request, "urlopen", side_effect=err):
            with pytest.raises(DevStageError) as exc_info:
                ds_client._request("DELETE", "/open-api-public/v1/devenvs/xxx", "AK", "SK")
        assert exc_info.value.not_found is False
