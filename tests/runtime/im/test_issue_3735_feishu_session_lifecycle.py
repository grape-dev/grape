"""Issue #3735：飞书渠道重建/停止后 aiohttp ClientSession 必须收尾关闭。

现场（grape-mgmt）：渠道批量重建后 +15s 出现等量 `Unclosed client session`，
1 次重建 ≈ 1 个未关闭会话，fd/连接池与内存随重建次数累积。

契约：
  1. 装配层丢弃 `(ctx, api)` 对（引擎 stop / 凭证覆盖重建 / 渠道让渡 prune）时，
     必须关闭其 `FeishuAsync` session；重复关闭幂等；
  2. 关闭失败只 WARNING（含 app_id），不向上抛、不阻塞装配；
  3. `FeishuChannel` 自身不持有 FeishuAsync（session 只属于短生命周期客户端）；
  4. `_default_endpoint_fetcher` 关闭失败不得覆盖原返回值/原异常。
"""
import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from src.domains.runtime.im import engine_wiring, feishu_async
from src.domains.runtime.im.adapters.feishu import channel as feishu_channel
from src.domains.runtime.im.adapters.feishu.outbound import FeishuSender
from src.domains.runtime.im.channel.channel import ChannelConfig
from src.domains.runtime.im.channel.types import Scope, Type
from src.domains.runtime.im.engine_wiring import ImEngine
from src.domains.runtime.im.feishu_async import FeishuAsync


def _engine(tmp_path) -> ImEngine:
    return ImEngine(str(tmp_path / "im.db"), instance_id="inst-3735")


class FakeApi:
    """装配层 api 替身：记录 close 调用次数，可注入关闭失败。"""

    def __init__(self, *, raise_on_close: bool = False) -> None:
        self.closed = False
        self.close_calls = 0
        self._raise = raise_on_close

    async def close(self) -> None:
        self.close_calls += 1
        if self._raise:
            raise RuntimeError("close boom")
        self.closed = True


# ---------------------------------------------------------------- 引擎 stop 收尾

async def test_engine_stop_closes_cached_feishu_sessions(tmp_path, monkeypatch):
    """stop() 后 ctx 缓存与进程级 feishu_api 缓存里的真实 session 全部 closed。"""
    engine = _engine(tmp_path)
    api = FeishuAsync()
    session = await api._get_session()          # 懒建真实 aiohttp.ClientSession
    engine._ctx_cache["cli_a"] = (SimpleNamespace(app_id="cli_a"), api)

    shared = FeishuAsync()
    shared_session = await shared._get_session()
    monkeypatch.setitem(engine_wiring._feishu_api_cache, "cli_b", shared)

    await engine.stop()

    assert session.closed is True
    assert shared_session.closed is True
    assert engine._ctx_cache == {}
    assert "cli_b" not in engine_wiring._feishu_api_cache


async def test_engine_stop_is_idempotent_for_api_close(tmp_path):
    """缓存已清空 → 第二次 stop 不再重复关闭。"""
    engine = _engine(tmp_path)
    api = FakeApi()
    engine._ctx_cache["cli_a"] = (SimpleNamespace(app_id="cli_a"), api)

    await engine.stop()
    await engine.stop()

    assert api.closed is True
    assert api.close_calls == 1


async def test_engine_stop_survives_api_close_failure(tmp_path, caplog):
    """关闭失败只 WARNING（带 app_id），stop 正常完成并清空缓存。"""
    engine = _engine(tmp_path)
    engine._ctx_cache["cli_boom"] = (
        SimpleNamespace(app_id="cli_boom"), FakeApi(raise_on_close=True))

    with caplog.at_level("WARNING"):
        await engine.stop()

    assert engine._ctx_cache == {}
    assert "cli_boom" in caplog.text


# ---------------------------------------------------------------- 让渡 / 重建收尾

async def test_prune_built_apps_closes_api_of_departed_channel(tmp_path):
    """渠道让渡/重建后 app 不在活渠道 → 关闭并移出 ctx 缓存。"""
    engine = _engine(tmp_path)
    engine.supervisor = cast(Any, SimpleNamespace(_channels={}))
    engine._built_apps["cli_gone"] = "inst-1"
    api = FakeApi()
    engine._ctx_cache["cli_gone"] = (SimpleNamespace(app_id="cli_gone"), api)

    engine._prune_built_apps()
    await asyncio.sleep(0.01)          # 关闭以 task 异步执行，不阻塞装配

    assert "cli_gone" not in engine._built_apps
    assert "cli_gone" not in engine._ctx_cache
    assert api.closed is True


async def test_prune_built_apps_keeps_live_channel_api(tmp_path):
    """活渠道的缓存 api 不得被误关。"""
    engine = _engine(tmp_path)
    engine.supervisor = cast(Any, SimpleNamespace(_channels={"inst-1": (object(), object())}))
    engine._built_apps["cli_live"] = "inst-1"
    api = FakeApi()
    engine._ctx_cache["cli_live"] = (SimpleNamespace(app_id="cli_live"), api)

    engine._prune_built_apps()
    await asyncio.sleep(0.01)

    assert engine._ctx_cache["cli_live"][1] is api
    assert api.close_calls == 0


async def test_drop_cached_ctx_closes_previous_api(tmp_path):
    """凭证覆盖重建（_api_overrides 换新）→ 旧 api 关闭并从缓存移除。"""
    engine = _engine(tmp_path)
    api = FakeApi()
    engine._ctx_cache["cli_x"] = (SimpleNamespace(app_id="cli_x"), api)

    engine._drop_cached_ctx("cli_x")
    await asyncio.sleep(0.01)

    assert "cli_x" not in engine._ctx_cache
    assert api.closed is True

    # 幂等：条目已不存在时不再重复关闭
    engine._drop_cached_ctx("cli_x")
    assert api.close_calls == 1


# ---------------------------------------------------------------- endpoint 客户端

def test_endpoint_close_failure_keeps_result(monkeypatch):
    """finally 里 close 失败不得覆盖 get_ws_endpoint 的返回值。"""

    class BoomApi:
        def __init__(self, *a, **kw):
            pass

        async def get_ws_endpoint(self, ctx):
            return "wss://example.invalid/ws", 0

        async def close(self):
            raise RuntimeError("close boom")

    monkeypatch.setattr(feishu_async, "FeishuAsync", BoomApi)

    assert asyncio.run(
        feishu_channel._default_endpoint_fetcher("cli_x", "secret")
    ) == ("wss://example.invalid/ws", 0)


# ---------------------------------------------------------------- 契约守卫

def test_channel_holds_no_feishu_async_instance():
    """渠道不得持有 FeishuAsync（session 归短生命周期客户端，重建即关）。"""
    cfg = ChannelConfig(type=Type.FEISHU, raw={"app_id": "cli_x"},
                        installation_id=UUID(int=1), scope=Scope.USER)
    sender = FeishuSender(lambda *a, **kw: "ok", lambda *a, **kw: "ok")
    channel = feishu_channel.FeishuChannel(cfg, sender)

    assert not any(isinstance(v, FeishuAsync) for v in vars(channel).values())