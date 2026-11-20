"""cloud_worker / claim_predicate / engine_select 核心逻辑测试。

覆盖范围（17 个测试用例）：

A. 模板展开（cloud_worker.main 的模板→runtime 条目展开逻辑）
   1. test_expand_single_template_single_engine
   2. test_expand_multi_engines
   3. test_expand_multiple_templates
   4. test_cubesandbox_priority_sort
   5. test_expand_runtime_name_format
   6. test_expand_runtime_info_json_fields
   7. test_expand_engines_json_string

B. _refresh_templates（心跳模板刷新，MD5 hash 对比）
   8. test_refresh_templates_no_change_no_reregister
   9. test_refresh_templates_changed_reregister
  10. test_refresh_templates_empty_list_skips
  11. test_refresh_templates_deleted_templates_set_offline

C. claim_predicate（LlmCredentialPredicate + ClaimPredicateChain）
  12. test_claim_decision_dataclass
  13. test_llm_credential_predicate_cloud_with_key
  14. test_llm_credential_predicate_cloud_no_key
  15. test_llm_credential_predicate_non_cloud_pass
  16. test_claim_chain_fail_open_on_exception

D. engine_select（engine_name + pick_engine 核心逻辑）
  17. test_engine_name_zero_returns_empty
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src/ is importable (conftest already does this, but be explicit for standalone)
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from src.domains.runtime.engine.claim_predicate import (
    ClaimDecision,
    ClaimPredicateChain,
    LlmCredentialPredicate,
)
from src.domains.runtime.engine.cloud_worker import _load_config, _refresh_templates


# ---------------------------------------------------------------------------
# Helpers — 复刻 cloud_worker.main 中的模板展开逻辑（纯函数，不依赖 client）
# ---------------------------------------------------------------------------

def _expand_templates(templates: list[dict]) -> list[dict]:
    """v4 复刻 cloud_worker._expand_templates 的模板展开逻辑，供纯函数测试。

    v4：四段式 name = f"{engine}:{tpl_slug}:{platform}:{provider_id}"
    （[多模板共用引擎] tpl_slug = 模板 name 规整化 / t{db_id} 兜底）
    """
    runtimes = []
    import re as _re
    def _re_slug(_s, max_len=16):
        _s = _re.sub(r"[^a-z0-9]+", "-", (_s or "").strip().lower()).strip("-")
        if not _s:
            return ""
        if len(_s) > max_len:
            _m = _re.search(r"(\d+)$", _s)
            _tail = _m.group(1) if _m else _s[-3:]
            _s = (_s[: max_len - len(_tail) - 1] + "-" + _tail)[:max_len].strip("-")
        return _s
    def _tpl_slug(t):
        return _re_slug(t.get("name") or "") or f"t{t.get('id', 0)}"
    _used_slugs: dict = {}
    for t in sorted(templates, key=lambda t: (
        0 if t.get("platform") == "cubesandbox" else 1,
        0 if t.get("scope") == "system" else 1,
        t.get("provider_id", 0),
        t.get("template_id", ""),
    )):
        _provider_id = t.get("provider_id", 0)
        _platform = t.get("platform", "") or t.get("provider", "")
        _scope = t.get("scope", "system")
        if not _platform or _platform == "disabled":
            continue
        _provider_name = t.get("provider_name", "") or _platform
        _prov_s = _re_slug(_provider_name) or _platform
        _template_id = t.get("template_id", "")
        _template_name = t.get("name", "")
        _tpl_db_id = t.get("id", 0) or 0
        _tpl_s = _tpl_slug(t)
        if _tpl_s in _used_slugs and _used_slugs[_tpl_s] != _tpl_db_id:
            _tpl_s = f"t{_tpl_db_id}"
        _used_slugs[_tpl_s] = _tpl_db_id
        _preinstalled = t.get("preinstalled", False)
        _description = t.get("description", "")
        _disk_size_mb = t.get("disk_size_mb", 0)
        engines_raw = t.get("engines", "[]")
        try:
            _engines = json.loads(engines_raw) if isinstance(engines_raw, str) else (engines_raw or [])
        except Exception:
            _engines = []
        _agent_tags = ["all"] if _scope == "system" else []
        for _engine_type in _engines:
            _ri = json.dumps({
                "engine_type": _engine_type,
                "platform": _platform,
                "provider_id": _provider_id,
                "provider_name": _provider_name,
                "template_id": _template_id,
                "template_db_id": _tpl_db_id,
                "template_name": _template_name,
                "preinstalled": _preinstalled,
                "description": _description,
                "disk_size_mb": _disk_size_mb,
            })
            runtimes.append({
                "name": f"{_engine_type}:{_tpl_s}:{_prov_s}:{_provider_id}",
                "version": "",
                "healthy": True,
                "installed": True,
                "cli_path": "",
                "provider": _provider_name,
                "runtime_type": "cloud",
                "runtime_info": _ri,
                "agent_tags": _agent_tags,
            })
    return runtimes


def _sort_cubesandbox_first(templates: list[dict]) -> list[dict]:
    """v3 复刻排序：cubesandbox 优先 + system scope 优先 + provider_id 升序。"""
    return sorted(templates, key=lambda t: (
        0 if t.get("platform") == "cubesandbox" else 1,
        0 if t.get("scope") == "system" else 1,
        t.get("provider_id", 0),
        t.get("template_id", ""),
    ))


# ===================================================================
# A. 模板展开
# ===================================================================

class TestTemplateExpansion:
    """模板 → runtime 条目展开逻辑。"""

    def test_expand_single_template_single_engine(self):
        """单模板单引擎 → 1 个 runtime 条目。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base",
             "engines": ["hermes"]},
        ]
        runtimes = _expand_templates(templates)
        assert len(runtimes) == 1
        assert runtimes[0]["name"] == "hermes:base:devbox:0"
        assert runtimes[0]["provider"] == "devbox"  # v3: provider_name from platform
        assert runtimes[0]["runtime_type"] == "cloud"
        assert runtimes[0]["healthy"] is True
        assert runtimes[0]["installed"] is True

    def test_expand_multi_engines(self):
        """单模板多引擎 → N 个 runtime 条目（engines × provider）。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base",
             "engines": ["hermes", "opencode", "codex"]},
        ]
        runtimes = _expand_templates(templates)
        assert len(runtimes) == 3
        names = [r["name"] for r in runtimes]
        assert "hermes:base:devbox:0" in names
        assert "opencode:base:devbox:0" in names
        assert "codex:base:devbox:0" in names

    def test_expand_multiple_templates(self):
        """多模板多引擎 → 所有模板展开后合并。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base",
             "engines": ["hermes"]},
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 1, "scope": "system", "template_id": "tpl-002", "name": "cube",
             "engines": ["hermes", "opencode"]},
        ]
        runtimes = _expand_templates(templates)
        assert len(runtimes) == 3
        # 确认两个模板的条目都存在
        providers = {r["provider"] for r in runtimes}
        assert "devbox" in providers
        assert "cubesandbox" in providers

    def test_cubesandbox_priority_sort(self):
        """cubesandbox 模板排在 devbox 之前（优先级排序）。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 1, "scope": "system", "template_id": "tpl-002", "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-003", "engines": ["opencode"]},
        ]
        sorted_templates = _sort_cubesandbox_first(templates)
        # cubesandbox 应排第一
        assert sorted_templates[0]["provider"] == "cubesandbox"
        # 其余 devbox 随后
        assert all(t["provider"] == "devbox" for t in sorted_templates[1:])

    def test_expand_runtime_name_format(self):
        """runtime name 格式 = agent_type:provider。"""
        templates = [
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 1, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
        ]
        runtimes = _expand_templates(templates)
        assert runtimes[0]["name"] == "hermes:t0:cubesandbox:1"

    def test_expand_runtime_info_json_fields(self):
        """runtime_info JSON 包含 agent_type/provider/template_id 等字段。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base-tpl",
             "preinstalled": True, "description": "base image", "disk_size_mb": 20480,
             "engines": ["hermes"]},
        ]
        runtimes = _expand_templates(templates)
        ri = json.loads(runtimes[0]["runtime_info"])
        assert ri["engine_type"] == "hermes"
        assert ri["platform"] == "devbox"
        assert ri["provider_id"] == 0
        assert ri["provider_name"] == "devbox"
        assert ri["template_id"] == "tpl-001"
        assert ri["template_name"] == "base-tpl"
        assert ri["preinstalled"] is True
        assert ri["description"] == "base image"
        assert ri["disk_size_mb"] == 20480

    def test_expand_engines_json_string(self):
        """engines 字段为 JSON 字符串时也能正确解析。"""
        templates = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001",
             "engines": '["hermes", "opencode"]'},
        ]
        runtimes = _expand_templates(templates)
        assert len(runtimes) == 2
        names = {r["name"] for r in runtimes}
        assert names == {"hermes:t0:devbox:0", "opencode:t0:devbox:0"}


# ===================================================================
# B. _refresh_templates
# ===================================================================

class TestRefreshTemplates:
    """_refresh_templates 心跳模板刷新——MD5 hash 对比。"""

    def _make_templates(self) -> list[dict]:
        return [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base",
             "engines": ["hermes"]},
        ]

    def _reset_refresh_state(self):
        """清除 _refresh_templates 的缓存 hash（测试间隔离）。"""
        if hasattr(_refresh_templates, "_last_hash"):
            delattr(_refresh_templates, "_last_hash")
        if hasattr(_refresh_templates, "_last_names"):
            delattr(_refresh_templates, "_last_names")
        if hasattr(_refresh_templates, "_last_runtimes"):
            delattr(_refresh_templates, "_last_runtimes")

    def setup_method(self):
        self._reset_refresh_state()

    @pytest.mark.asyncio
    async def test_refresh_templates_no_change_no_reregister(self):
        """模板未变化时不重注册（hash 相同）。"""
        templates = self._make_templates()
        config = {"device_name": "test-device"}

        client = MagicMock()
        client.get_templates = AsyncMock(return_value=templates)
        client.register = AsyncMock(return_value=([1], ""))

        # 第一次调用——注册
        await _refresh_templates(client, config)
        assert client.register.call_count == 1

        # 第二次调用——模板相同，不重注册
        await _refresh_templates(client, config)
        assert client.register.call_count == 1  # 仍然只注册过 1 次

    @pytest.mark.asyncio
    async def test_refresh_templates_changed_reregister(self):
        """模板变化时重注册（hash 不同）。"""
        templates_v1 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
        ]
        templates_v2 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes", "opencode"]},
        ]
        config = {"device_name": "test-device"}

        client = MagicMock()
        client.get_templates = AsyncMock(side_effect=[templates_v1, templates_v2])
        client.register = AsyncMock(return_value=([1, 2], ""))

        # 第一次
        await _refresh_templates(client, config)
        assert client.register.call_count == 1

        # 第二次——模板变了（多了 opencode），应重注册
        await _refresh_templates(client, config)
        assert client.register.call_count == 2

    @pytest.mark.asyncio
    async def test_refresh_templates_empty_list_skips(self):
        """空模板列表直接返回，不注册。"""
        config = {"device_name": "test-device"}
        client = MagicMock()
        client.get_templates = AsyncMock(return_value=[])

        await _refresh_templates(client, config)
        client.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_templates_new_runtime_returns_rid_map(self):
        """回归：「生而不养」缺陷（2026-10-28 8081 实证）——增量注册的新引擎
        必须把新 rid 通过返回值交给调用方，否则新 runtime 注册进 DB 后无心跳，
        100s 内被服务端 reap 判 offline。

        场景：第一轮只有 hermes 引擎 → 第二轮模板新增 opencode 引擎，
        _refresh_templates 应返回 {新引擎名: rid} 映射（只含新增条目，
        runtime_info 变化的老条目不返回）。
        """
        templates_v1 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
        ]
        templates_v2 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system", "template_id": "tpl-opencode", "engines": ["opencode"]},
        ]
        config = {"device_name": "test-device"}

        client = MagicMock()
        client.get_templates = AsyncMock(side_effect=[templates_v1, templates_v2])
        # 第一轮注册 hermes → rid 4；第二轮新增 opencode → rid 7
        client.register = AsyncMock(side_effect=[([4], ""), ([7], "")])

        # 第一轮
        new_map_1, deleted_1, _rt1 = await _refresh_templates(client, config)
        assert client.register.call_count == 1

        # 第二轮——新增 opencode:devbox:3，返回值应含新 rid
        new_map_2, deleted_2, _rt2 = await _refresh_templates(client, config)
        assert client.register.call_count == 2
        assert new_map_2 == {"opencode:t0:devbox:3": 7}, \
            f"新增引擎必须返回 rid 映射供心跳列表 append，got {new_map_2}"
        assert deleted_2 == set()

    @pytest.mark.asyncio
    async def test_refresh_templates_changed_info_returns_empty_map(self):
        """runtime_info 变化（非新增）的老引擎重注册——rid 不变，不进 new_map。"""
        templates_v1 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"], "description": "old"},
        ]
        templates_v2 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"], "description": "new"},
        ]
        config = {"device_name": "test-device"}

        client = MagicMock()
        client.get_templates = AsyncMock(side_effect=[templates_v1, templates_v2])
        client.register = AsyncMock(side_effect=[([4], ""), ([4], "")])

        await _refresh_templates(client, config)
        new_map, deleted, _rts = await _refresh_templates(client, config)
        assert client.register.call_count == 2  # 重注册了（info 变化）
        assert new_map == {}, "老引擎 info 变化不该进 new_map（rid 已在心跳列表）"
        assert deleted == set()

    @pytest.mark.asyncio
    async def test_refresh_templates_deleted_templates_set_offline(self):
        """模板被删除时，对应的旧 runtime 应标 offline（healthy=False）。"""
        templates_v1 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-002", "engines": ["opencode"]},
        ]
        templates_v2 = [
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
            # tpl-002 (opencode:devbox) 被删除
        ]
        config = {"device_name": "test-device"}

        client = MagicMock()
        client.get_templates = AsyncMock(side_effect=[templates_v1, templates_v2])
        client.register = AsyncMock(return_value=([1, 2], ""))

        # 第一次——注册 2 个 runtime
        await _refresh_templates(client, config)
        first_call_count = client.register.call_count

        # 第二次——tpl-002 删除，应额外调 register 标 offline
        await _refresh_templates(client, config)
        assert client.register.call_count > first_call_count

        # 最后一次 register 调用应包含 offline runtime（opencode:devbox）
        last_call_args = client.register.call_args
        last_runtimes = last_call_args.kwargs.get("runtimes") or last_call_args.args[0]
        offline_names = [r["name"] for r in last_runtimes if not r.get("healthy", True)]
        assert "opencode:t0:devbox:0" in offline_names


# ===================================================================
# C. claim_predicate
# ===================================================================

class TestClaimPredicate:
    """ClaimPredicateChain + LlmCredentialPredicate。"""

    def test_claim_decision_dataclass(self):
        """ClaimDecision 是 frozen dataclass，有 allowed + reason 字段。"""
        d1 = ClaimDecision(allowed=True)
        assert d1.allowed is True
        assert d1.reason == ""

        d2 = ClaimDecision(allowed=False, reason="no key")
        assert d2.allowed is False
        assert d2.reason == "no key"

        # frozen——不可变
        with pytest.raises(AttributeError):
            d1.allowed = False

    def test_llm_credential_predicate_cloud_with_key(self):
        """cloud 任务 + 用户有 LLM Key → allowed。"""
        mock_reader = MagicMock()
        mock_reader.query_llm_env.return_value = {
            "_LLM_API_KEY": "sk-test-123",
            "_LLM_PROVIDER": "openai",
        }
        pred = LlmCredentialPredicate(credential_reader=mock_reader)
        task = {"runtime_type": "cloud", "user_id": 42}
        decision = pred.check(task, runtime_id=1)
        assert decision.allowed is True
        mock_reader.query_llm_env.assert_called_once_with(42, "")

    def test_llm_credential_predicate_cloud_no_key(self):
        """cloud 任务 + 用户无 LLM Key → 拒绝。"""
        mock_reader = MagicMock()
        mock_reader.query_llm_env.return_value = {"_LLM_API_KEY": ""}
        pred = LlmCredentialPredicate(credential_reader=mock_reader)
        task = {"runtime_type": "cloud", "user_id": 42}
        decision = pred.check(task, runtime_id=1)
        assert decision.allowed is False
        assert "no LLM credentials" in decision.reason

    def test_llm_credential_predicate_non_cloud_pass(self):
        """非 cloud 任务（local/user）直接通过，不查 credential。"""
        mock_reader = MagicMock()
        pred = LlmCredentialPredicate(credential_reader=mock_reader)
        task = {"runtime_type": "local", "user_id": 42}
        decision = pred.check(task, runtime_id=1)
        assert decision.allowed is True
        mock_reader.query_llm_env.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_chain_fail_open_on_exception(self):
        """Predicate 异常时 fail-open → allowed=True。"""
        # 构造一个会抛异常的 predicate
        class BoomPredicate:
            def check(self, task, runtime_id):
                raise RuntimeError("DB connection lost")

        chain = ClaimPredicateChain([BoomPredicate()])
        decision = chain.check({"user_id": 1}, runtime_id=1)
        assert decision.allowed is True  # fail-open


# ===================================================================
# D. engine_select
# ===================================================================

class TestEngineSelect:
    """engine_select 核心逻辑。"""

    def test_engine_name_zero_returns_empty(self):
        """engine_name(runtime_id=0) 返回空串（不查 DB）。"""
        from src.domains.runtime.engine.engine_select import engine_name
        assert engine_name(0) == ""


# ===================================================================
# E. _expand_templates 边界场景（测试 src.domains.runtime.engine.cloud_worker._expand_templates 真实函数）
# ===================================================================

class TestTemplateExpansionEdgeCases:
    """_expand_templates 边界场景——测试 src.domains.runtime.engine.cloud_worker 中的真实函数。

    覆盖审查报告建议的边界：
    - engines 为空 / 缺失
    - engines 非法 JSON
    - provider 为 disabled / 空串
    - 多个模板同一 provider
    """

    def _expand(self, templates):
        """直接调用 src 中的真实 _expand_templates 函数。"""
        from src.domains.runtime.engine.cloud_worker import _expand_templates
        return _expand_templates(templates)

    def test_engines_empty_list(self):
        """engines=[] → 0 个 runtime 条目（空数组不展开）。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": []},
        ])
        assert runtimes == []

    def test_engines_missing(self):
        """模板不含 engines 字段 → 默认 []，0 个条目。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001"},
        ])
        assert runtimes == []

    def test_engines_invalid_json_string(self):
        """engines 为非法 JSON 字符串 → json.loads 抛异常 → 降级为 []，0 个条目。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": "{invalid json}"},
        ])
        assert runtimes == []

    def test_engines_invalid_json_partial(self):
        """engines 为截断的 JSON 字符串 → 同样降级为 []。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": "[\"hermes\","},
        ])
        assert runtimes == []

    def test_provider_disabled_skipped(self):
        """provider="disabled" → 跳过，0 个条目。"""
        runtimes = self._expand([
            {"provider": "disabled", "platform": "disabled", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
        ])
        assert runtimes == []

    def test_provider_empty_string_skipped(self):
        """provider="" → 跳过，0 个条目。"""
        runtimes = self._expand([
            {"provider": "", "template_id": "tpl-001", "engines": ["hermes"]},
        ])
        assert runtimes == []

    def test_multiple_templates_same_provider(self):
        """多个模板同一 provider → 各自展开，不合并不覆盖。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "name": "base",
             "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-002", "name": "full",
             "engines": ["opencode", "codex"]},
        ])
        assert len(runtimes) == 3
        # 两个模板的条目都存在
        names = [r["name"] for r in runtimes]
        assert "hermes:base:devbox:0" in names
        assert "opencode:full:devbox:0" in names
        assert "codex:full:devbox:0" in names
        # runtime_info 中 template_id 各自正确
        for r in runtimes:
            ri = json.loads(r["runtime_info"])
            if ri["engine_type"] == "hermes":
                assert ri["template_id"] == "tpl-001"
                assert ri["template_name"] == "base"
            elif ri["engine_type"] == "opencode":
                assert ri["template_id"] == "tpl-002"
                assert ri["template_name"] == "full"
            elif ri["engine_type"] == "codex":
                assert ri["template_id"] == "tpl-002"

    def test_mixed_disabled_and_valid(self):
        """混合 disabled 和有效模板 → 只展开有效模板。"""
        runtimes = self._expand([
            {"provider": "disabled", "platform": "disabled", "provider_id": 0, "scope": "system", "template_id": "tpl-x", "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["opencode"]},
        ])
        assert len(runtimes) == 1
        assert runtimes[0]["name"] == "opencode:t0:devbox:0"

    def test_runtime_info_uses_engine_type_not_agent_type(self):
        """runtime_info JSON 中字段名为 engine_type（不是 agent_type）。

        回归锚点：测试文件早期的本地 _expand_templates 辅助用 agent_type，
        但 src 中的真实函数用 engine_type。确保测试真实函数的字段名。
        """
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
        ])
        ri = json.loads(runtimes[0]["runtime_info"])
        assert "engine_type" in ri
        assert ri["engine_type"] == "hermes"
        # agent_type 不应存在（真实函数的 schema）
        assert "agent_type" not in ri

    def test_cubesandbox_priority_sort_in_expand(self):
        """_expand_templates 内部已做 cubesandbox 优先排序。"""
        runtimes = self._expand([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system", "template_id": "tpl-001", "engines": ["hermes"]},
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 1, "scope": "system", "template_id": "tpl-002", "engines": ["hermes"]},
        ])
        # cubesandbox 应排第一（内部 sorted）
        assert runtimes[0]["provider"] == "cubesandbox"
        assert runtimes[1]["provider"] == "devbox"


class TestMultiTemplatePerEngine:
    """[多模板共用引擎] 四段式引擎名 + 同 Provider 多模板同引擎（spec 验收 1/2）。"""

    def test_same_engine_two_templates_two_rows(self):
        """同 Provider 下 T1/T2 都绑 hermes → 两个四段名引擎行（不合并不覆盖）。"""
        runtimes = _expand_templates([
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 38, "scope": "user",
             "template_id": "tpl-a", "id": 25, "name": "hermes-git-acp", "engines": ["hermes"]},
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 38, "scope": "user",
             "template_id": "tpl-b", "id": 29, "name": "hermes-git", "engines": ["hermes"]},
        ])
        assert len(runtimes) == 2
        names = {r["name"] for r in runtimes}
        assert names == {"hermes:hermes-git-acp:cubesandbox:38", "hermes:hermes-git:cubesandbox:38"}

    def test_slug_collision_falls_back_to_db_id(self):
        """两模板 name 规整后同 slug → t{db_id} 兜底保证互异。"""
        runtimes = _expand_templates([
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system",
             "template_id": "tpl-1", "id": 11, "name": "My Template!", "engines": ["hermes"]},
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system",
             "template_id": "tpl-2", "id": 12, "name": "my-template", "engines": ["hermes"]},
        ])
        names = [r["name"] for r in runtimes]
        assert len(set(names)) == 2, f"slug 撞名未兜底: {names}"
        # 第一个占 slug，第二个 t12
        assert "hermes:my-template:devbox:3" in names
        assert "hermes:t12:devbox:3" in names

    def test_empty_name_uses_db_id(self):
        """name 为空 → t{id}。"""
        runtimes = _expand_templates([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system",
             "template_id": "tpl-x", "id": 7, "engines": ["opencode"]},
        ])
        assert runtimes[0]["name"] == "opencode:t7:devbox:0"

    def test_runtime_info_carries_template_db_id(self):
        """runtime_info 带 template_db_id（身份锚定用，后续增强）。"""
        import json as _j
        runtimes = _expand_templates([
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system",
             "template_id": "tpl-1", "id": 11, "name": "base", "engines": ["hermes"]},
        ])
        ri = _j.loads(runtimes[0]["runtime_info"])
        assert ri["template_db_id"] == 11

    def test_long_name_slug_truncated_keep_tail_digits(self):
        """超长 name → 16 字符截断保尾数字（同 sanitize_device 语义）。"""
        runtimes = _expand_templates([
            {"provider": "devbox", "platform": "devbox", "provider_id": 0, "scope": "system",
             "template_id": "tpl-l", "id": 1, "name": "a-very-long-template-name-0007", "engines": ["hermes"]},
        ])
        seg = runtimes[0]["name"].split(":")[1]
        assert len(seg) <= 16 and seg.endswith("0007"), seg

    def test_provider_name_segment(self):
        """[引擎名第三段=Provider 名] provider_name 与平台名不同时，名用 Provider 名。"""
        runtimes = _expand_templates([
            {"provider": "cubesandbox", "platform": "cubesandbox", "provider_id": 38, "scope": "user",
             "provider_name": "cubesandbox-sbx-e2e", "template_id": "tpl-a", "id": 25, "name": "hermes-git-acp",
             "engines": ["hermes"]},
        ])
        # cubesandbox-sbx-e2e(19字) → 16 字符保尾 e2e 截断
        assert runtimes[0]["name"] == "hermes:hermes-git-acp:cubesandbox--e2e:38"

    def test_provider_name_fallback_platform(self):
        """provider_name 为空 → 回落平台名。"""
        runtimes = _expand_templates([
            {"provider": "devbox", "platform": "devbox", "provider_id": 3, "scope": "system",
             "template_id": "tpl-1", "id": 11, "name": "base", "engines": ["hermes"]},
        ])
        assert runtimes[0]["name"] == "hermes:base:devbox:3"
