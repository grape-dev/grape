"""云模式重注册守卫（2026-11-11 孤儿态根因修复）。

背景：服务端重启 → worker 心跳连败≥3 / 5min 定期重注册 → _reregister 原逻辑
无条件本地重发现（Issue #471 C1，为本机 worker 设计）→ cloud_worker 复用
_run_loop 时云引擎心跳列表被本机引擎整体替换：
  ① 云引擎挤出心跳列表 → 服务端 100s reap 永久 offline（孤儿态）
  ② 本机引擎误注沙箱凭据下（cred15 污染行实测）
  ③ 模板 hash 未变 → _refresh_templates 不再重注册 → 非重启不可恢复
修复：prev_installed 含云引擎 → 重锚原云列表（name 锚定注册拿回原 rid）。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _cloud_entry(name="hermes:hermes-git-acp:s2:4", rid=25):
    return {"name": name, "version": "1.0", "healthy": True, "installed": True,
            "cli_path": "", "provider": "cubesandbox", "runtime_type": "cloud",
            "runtime_info": '{"engine_type": "hermes"}'}


def _local_entry(name="hermes"):
    return {"name": name, "version": "1.0", "healthy": True, "installed": True,
            "cli_path": "/usr/local/bin/hermes", "provider": "", "runtime_type": "local",
            "runtime_info": "{}"}


@pytest.fixture
def client():
    c = MagicMock()
    c.reset_session = AsyncMock()
    c.register = AsyncMock(return_value=([25], {"runtimes": []}))
    c.recover_orphans = AsyncMock()
    c.runtimes_detail = AsyncMock(return_value={"runtimes": []})
    return c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestCloudModeReregister:
    def test_cloud_prev_reanchors_cloud_list(self, client):
        """云模式：重注册的 installed 必须是原云列表（逐条相等），不走本地发现。"""
        from src.domains.runtime.engine.agent import _reregister
        prev = [_cloud_entry()]
        _run(_reregister(client, prev, "dev", cli_version="1.0"))
        registered = client.register.call_args[0][0]
        assert registered == prev, f"云模式重注册必须锚定原列表, 实际: {registered}"
        assert all(r["runtime_type"] == "cloud" for r in registered)

    def test_cloud_prev_never_local_discovery(self, client):
        """云模式：绝不调用 _discover_local_runtimes（污染与挤出双根因）。"""
        from src.domains.runtime.engine.agent import _reregister
        with patch("src.domains.runtime.engine.agent._discover_local_runtimes",
                   side_effect=AssertionError("云模式不得本地重发现")) as _disc:
            _run(_reregister(client, [_cloud_entry()], "dev", cli_version="1.0"))
            _disc.assert_not_called()

    def test_local_mode_keeps_c1_rediscovery(self, client):
        """本机 worker 模式（prev 全 local）：保持 C1 本地重发现语义不变。"""
        from src.domains.runtime.engine.agent import _reregister
        local_found = [_local_entry("opencode"), _local_entry("aishell-dsh")]
        with patch("src.domains.runtime.engine.agent._discover_local_runtimes",
                   return_value=local_found), \
             patch("src.domains.runtime.engine.agent._scan_local_skills", return_value=[]):
            _run(_reregister(client, [_local_entry()], "dev", cli_version="1.0"))
        registered = client.register.call_args[0][0]
        assert registered == local_found, "本机模式 C1 重发现语义不得变化"

    def test_empty_prev_keeps_c1(self, client):
        """prev 为空（本机 worker 启动无引擎快照）：沿用 C1 本地重发现。"""
        from src.domains.runtime.engine.agent import _reregister
        local_found = [_local_entry()]
        with patch("src.domains.runtime.engine.agent._discover_local_runtimes",
                   return_value=local_found), \
             patch("src.domains.runtime.engine.agent._scan_local_skills", return_value=[]):
            _run(_reregister(client, [], "dev", cli_version="1.0"))
        assert client.register.call_args[0][0] == local_found

    def test_cloud_returns_original_rids(self, client):
        """云模式重注册拿回原 rid（心跳列表整体替换后仍指同一批行）。"""
        from src.domains.runtime.engine.agent import _reregister
        client.register = AsyncMock(return_value=([22, 25], {}))
        rr = _run(_reregister(client, [_cloud_entry("op:x"), _cloud_entry("h:x")], "dev", cli_version="1.0"))
        assert rr is not None
        runtime_ids, _ebr, installed = rr
        assert runtime_ids == [22, 25]
