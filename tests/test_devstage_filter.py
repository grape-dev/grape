"""Tests for DevStage environment template filtering.

Verifies that list_environments only returns environments created with the
Grape AI Devspace template, filtering out environments from other templates.
"""
import asyncio
import json
import pytest
from unittest.mock import patch

from src.api.routes import devstage as ds_route
from src.im import devstage_client as ds_client

GRAPE_TPL = "4ef59a2b4d3d4909aacd80f6007c00b2"
OTHER_TPL = "ffffffffffffffffffffffffffffffff"


def _make_env(eid, tpl_id="", name="env", env_vars=None):
    env = {"id": eid, "name": name, "status": "cde.0004", "type": "Container"}
    if tpl_id:
        env["template_id"] = tpl_id
    if env_vars is not None:
        env["_env_vars"] = env_vars
    return env


def _make_user():
    return {"username": "test_user"}


def _mock_ak_sk():
    return ("AKTEST", "SKTEST")


GRAPE_ENV_VARS = {"GRAPE_DEV_SERVER": "http://example.com", "GRAPE_DEV_TOKEN": "rta_xxx"}


class TestListEnvironmentsFilter:
    """Route-level: /api/devstage/environments should only return Grape-created envs."""

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_only_grape_created_envs_returned(self, mock_list, mock_aksk):
        """Envs with Grape template + GRAPE_DEV_SERVER pass; others filtered out."""
        mock_list.return_value = [
            # Grape 创建：有模板 + 有 token → 保留
            _make_env("env-1", tpl_id=GRAPE_TPL, name="grape-env", env_vars=GRAPE_ENV_VARS),
            # 非 Grape 模板 → 过滤
            _make_env("env-2", tpl_id=OTHER_TPL, name="other-env", env_vars=GRAPE_ENV_VARS),
            # Grape 模板但控制台手动创建（无 token） → 过滤
            _make_env("env-3", tpl_id=GRAPE_TPL, name="manual-env", env_vars={}),
            # Grape 创建：有模板 + 有 token → 保留
            _make_env("env-4", tpl_id=GRAPE_TPL, name="grape-env-2", env_vars=GRAPE_ENV_VARS),
        ]
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        ids = [e["id"] for e in result["environments"]]
        assert ids == ["env-1", "env-4"]

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_console_manual_env_with_grape_template_filtered_out(self, mock_list, mock_aksk):
        """Grape 模板但无 GRAPE_DEV_SERVER（控制台手动创建）→ 过滤掉。"""
        mock_list.return_value = [
            _make_env("manual", tpl_id=GRAPE_TPL, name="manual", env_vars={}),
            _make_env("grape", tpl_id=GRAPE_TPL, name="grape", env_vars=GRAPE_ENV_VARS),
        ]
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        ids = [e["id"] for e in result["environments"]]
        assert ids == ["grape"]

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_all_grape_template_but_no_token_falls_back_to_tpl_only(self, mock_list, mock_aksk):
        """全部是 Grape 模板但都没有 GRAPE_DEV_SERVER → 回退到仅按 template_id 过滤。"""
        mock_list.return_value = [
            _make_env("env-1", tpl_id=GRAPE_TPL, name="manual-1", env_vars={}),
            _make_env("env-2", tpl_id=GRAPE_TPL, name="manual-2", env_vars={}),
        ]
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        ids = [e["id"] for e in result["environments"]]
        # 回退到仅按 template_id 匹配，两个都保留
        assert ids == ["env-1", "env-2"]

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_all_filtered_out_falls_back_to_full_list(self, mock_list, mock_aksk):
        """无 template_id 且无 env_vars → 全部回退。"""
        mock_list.return_value = [
            _make_env("env-x", tpl_id="", name="no-template-field"),
        ]
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        envs = result["environments"]
        assert len(envs) == 1
        assert envs[0]["id"] == "env-x"

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_empty_list_returns_empty(self, mock_list, mock_aksk):
        """Empty input → empty output (no fallback)."""
        mock_list.return_value = []
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        assert result["environments"] == []

    @patch.object(ds_route, "_get_ak_sk", return_value=_mock_ak_sk())
    @patch.object(ds_route, "_list_envs")
    def test_envs_without_template_id_filtered_out(self, mock_list, mock_aksk):
        """Environments missing template_id entirely are excluded (when other envs match)."""
        mock_list.return_value = [
            _make_env("grape-1", tpl_id=GRAPE_TPL, name="grape", env_vars=GRAPE_ENV_VARS),
            _make_env("mystery", tpl_id="", name="unknown-tpl"),
        ]
        result = asyncio.run(ds_route.list_environments(user=_make_user()))
        ids = [e["id"] for e in result["environments"]]
        assert ids == ["grape-1"]


class TestClientTemplateIdExtraction:
    """Client-level: list_environments should extract template_id from v1 detail content JSON."""

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_template_id_and_env_vars_from_v1_detail(self, mock_norm, mock_req):
        """v1 detail content JSON 中同时提取 template_id 和 environment_variables。"""
        v2_response = [{"id": "env-1", "name": "test", "status": "cde.0004"}]
        v1_detail = {
            "status": 4,
            "content": json.dumps({"content": {
                "template_id": GRAPE_TPL,
                "environment_variables": {"GRAPE_DEV_SERVER": "http://example.com"},
            }}),
        }
        mock_req.side_effect = [v2_response, v1_detail]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0]["template_id"] == GRAPE_TPL
        assert result[0]["_env_vars"]["GRAPE_DEV_SERVER"] == "http://example.com"

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_console_manual_env_empty_env_vars(self, mock_norm, mock_req):
        """控制台手动创建的环境 environment_variables 为空 {}。"""
        v2_response = [{"id": "env-1", "name": "manual", "status": "cde.0004"}]
        v1_detail = {
            "status": 4,
            "content": json.dumps({"content": {
                "template_id": GRAPE_TPL,
                "environment_variables": {},
            }}),
        }
        mock_req.side_effect = [v2_response, v1_detail]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0]["template_id"] == GRAPE_TPL
        assert result[0]["_env_vars"] == {}

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_template_id_preserved_from_v2(self, mock_norm, mock_req):
        """When v2 list already has template_id, v1 detail is not used to overwrite."""
        v2_response = [{"id": "env-1", "name": "test", "status": "cde.0004",
                        "template_id": OTHER_TPL}]
        v1_detail = {
            "status": 4,
            "content": json.dumps({"content": {"template_id": GRAPE_TPL}}),
        }
        mock_req.side_effect = [v2_response, v1_detail]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0]["template_id"] == OTHER_TPL

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_v1_detail_empty_content(self, mock_norm, mock_req):
        """v1 detail with empty content string → template_id stays empty."""
        v2_response = [{"id": "env-1", "name": "test", "status": "cde.0004"}]
        v1_detail = {"status": 4, "content": ""}
        mock_req.side_effect = [v2_response, v1_detail]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0].get("template_id", "") == ""

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_v1_detail_malformed_content(self, mock_norm, mock_req):
        """v1 detail with malformed JSON content → template_id stays empty, no crash."""
        v2_response = [{"id": "env-1", "name": "test", "status": "cde.0004"}]
        v1_detail = {"status": 4, "content": "not-valid-json"}
        mock_req.side_effect = [v2_response, v1_detail]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0].get("template_id", "") == ""

    @patch.object(ds_client, "_request")
    @patch.object(ds_client, "_normalize_status", side_effect=lambda e: e)
    def test_v1_detail_failure_no_crash(self, mock_norm, mock_req):
        """If v1 detail request fails, template_id stays empty, no crash."""
        v2_response = [{"id": "env-1", "name": "test", "status": "cde.0004"}]
        mock_req.side_effect = [v2_response, Exception("network error")]
        result = ds_client.list_environments("AK", "SK")
        assert len(result) == 1
        assert result[0].get("template_id", "") == ""
