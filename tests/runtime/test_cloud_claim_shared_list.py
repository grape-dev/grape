"""云 worker 领取链路回归：共享 list 契约 + 失败不覆写清单 + 云模式禁用本地扫描。

契约（不依赖具体实例）：云沙箱 worker 的引擎行可以在线、心跳正常、注册按周期
重复，但如果领取列表被置空，整台 worker 一条 get_tags/claim 都不会发，事件
永远 queued。根因两条：

1. `_run_loop` 拿到重注册结果后写成 `runtime_ids = _rrc["runtime_ids"]`（重新
   绑定新 list）。cloud_worker 把启动时的 ids 交给 run_loop，同时后台模板刷新
   任务往**同一个 list 对象**上 append 新引擎 rid；一旦主循环换绑，后台的
   append 全部落进被遗弃的旧 list → 模板后落的云引擎永远进不了领取列表。
2. `_refresh_templates` 拉取失败时返回 `([], set(), [])`，调用方无条件
   `_installed_live[:] = _all_runtimes or []` → 一次 499/超时就把已注册清单清空
   → 下一轮 register([]) → ids=[] → runtime_ids 归零 → 再无 claim。

本文件锁住三条不变式。第 1 条（原地更新）在 `_run_loop` 内部，靠
`test_cloud_run_loop_passes_shared_runtime_ids` 保证"交给 run_loop 的就是同一个
对象"，配合源码里的 `runtime_ids[:] = ...` 构成完整契约。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domains.runtime.engine import agent as agent_mod
from src.domains.runtime.engine import cloud_worker as cw


def _tpl(engine="hermes", tid="tpl-001"):
    return {
        "provider": "devbox", "platform": "devbox", "provider_id": 3,
        "scope": "system", "template_id": tid, "name": tid, "engines": [engine],
    }


class TestFastInstallTriggeredCloudGuard:
    def test_cloud_mode_never_scans_local_path(self, monkeypatch):
        """云模式下本地 PATH 扫描与本机引擎列表不可比——不许触发重注册。"""
        monkeypatch.setenv("GRAPE_RUNTIME_MODE", "cloud_worker")
        monkeypatch.setattr(agent_mod, "_scan_installed_cheap",
                            lambda: [{"name": "hermes", "installed": True, "cli_path": ""}])
        installed = [{"name": "hermes:tpl-x:test-sandbox:pid", "runtime_type": "cloud"}]
        assert agent_mod._fast_install_triggered(installed) == []

    def test_cloud_mode_via_provider_env(self, monkeypatch):
        monkeypatch.delenv("GRAPE_RUNTIME_MODE", raising=False)
        monkeypatch.setenv("CLOUD_WORKER_PROVIDER", "cubesandbox")
        monkeypatch.setattr(agent_mod, "_scan_installed_cheap",
                            lambda: [{"name": "opencode", "installed": True, "cli_path": ""}])
        assert agent_mod._fast_install_triggered([]) == []

    def test_local_mode_still_detects_new_engine(self, monkeypatch):
        monkeypatch.delenv("GRAPE_RUNTIME_MODE", raising=False)
        monkeypatch.delenv("CLOUD_WORKER_PROVIDER", raising=False)
        monkeypatch.setattr(agent_mod, "_scan_installed_cheap",
                            lambda: [{"name": "opencode", "installed": True, "cli_path": ""}])
        assert agent_mod._fast_install_triggered([])


class TestRefreshTemplatesFailureSignal:
    @pytest.mark.asyncio
    async def test_empty_templates_is_empty_not_none(self):
        """平台确实没有模板（[]）是真空状态，必须允许调用方同步清空已注册清单；
        只有『拉取失败/未知』才用 None 哨兵保留旧快照。"""
        client = MagicMock()
        client.get_templates = AsyncMock(return_value=[])
        rid_map, deleted, runtimes = await cw._refresh_templates(client, {"device_name": "d"})
        assert rid_map == {}
        assert deleted == set()
        assert runtimes == [], "真空模板必须返回 []，调用方据此清空引擎清单"
        client.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_templates_is_unknown_not_empty(self):
        """get_templates 返回 None（未知）与 [] 语义不同：必须保留旧快照。"""
        client = MagicMock()
        client.get_templates = AsyncMock(return_value=None)
        rid_map, deleted, runtimes = await cw._refresh_templates(client, {"device_name": "d"})
        assert (rid_map, deleted, runtimes) == (None, set(), None)

    @pytest.mark.asyncio
    async def test_exception_is_none_not_empty_list(self):
        client = MagicMock()
        client.get_templates = AsyncMock(side_effect=RuntimeError("499 / timeout"))
        rid_map, deleted, runtimes = await cw._refresh_templates(client, {"device_name": "d"})
        assert (rid_map, deleted, runtimes) == (None, set(), None)

    @pytest.mark.asyncio
    async def test_success_still_returns_runtimes(self):
        client = MagicMock()
        client.get_templates = AsyncMock(return_value=[_tpl()])
        client.register = AsyncMock(return_value=([42], ""))
        rid_map, deleted, runtimes = await cw._refresh_templates(client, {"device_name": "d"})
        assert runtimes and runtimes[0]["name"].startswith("hermes:")
        assert rid_map == {"hermes:tpl-001:devbox:3": 42}


class TestSharedRuntimeIdsContract:
    @pytest.mark.asyncio
    async def test_cloud_run_loop_passes_shared_runtime_ids(self, monkeypatch):
        """cloud_worker 必须把同一个 list 对象交给 run_loop（后台刷新要原地 append）。

        run_loop 内部再用 `runtime_ids[:] = ...` 原地更新才保住这个引用。
        """
        captured = {}

        async def fake_run_loop(client, runtime_ids, **kwargs):
            captured["obj_is_same"] = runtime_ids is ids
            captured["kwargs_installed_is_live"] = kwargs.get("installed") is not None
            # 模拟后台模板刷新 append 新 rid —— 必须出现在同一个对象里
            runtime_ids.append(17860)

        ids = [17859]
        monkeypatch.setattr(agent_mod, "_run_loop", fake_run_loop)
        cfg = {"poll_interval": 1, "heartbeat_interval": 1, "max_concurrent": 1}
        await cw._cloud_run_loop(MagicMock(), ids, cfg, {}, [], "dev")

        assert captured["obj_is_same"] is True
        assert captured["kwargs_installed_is_live"] is True
        assert 17860 in ids


class TestEmptyIdsWarnInterval:
    """节流间隔必须具名且可运维覆盖（架构守护 B2：不许裸字面量）。"""

    def test_default_is_named_constant(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_EMPTY_IDS_WARN_INTERVAL", raising=False)
        assert agent_mod._empty_ids_warn_interval() == agent_mod.EMPTY_IDS_WARN_INTERVAL_DEFAULT
        assert agent_mod.EMPTY_IDS_WARN_INTERVAL_DEFAULT > 0

    def test_env_override_is_honored(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_EMPTY_IDS_WARN_INTERVAL", "5")
        assert agent_mod._empty_ids_warn_interval() == 5.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        for bad in ("", "abc", "0", "-3"):
            monkeypatch.setenv("RUNTIME_EMPTY_IDS_WARN_INTERVAL", bad)
            assert agent_mod._empty_ids_warn_interval() == agent_mod.EMPTY_IDS_WARN_INTERVAL_DEFAULT
