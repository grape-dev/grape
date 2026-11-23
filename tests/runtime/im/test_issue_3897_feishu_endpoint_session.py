"""Issue #3897 D 组（B）：飞书 WS endpoint 取用不再泄漏 aiohttp session。

现场（grape-mgmt，1 小时 12 条）：`Unclosed client session`。
根因：`FeishuChannel.connect` 为取动态 WS endpoint 现场 `FeishuAsync()`（自建
`aiohttp.ClientSession`），拿到 URL 后既不复用也不 close —— 每个渠道实例（含
配置热更新重建、重连重建）泄漏一个连接池，进程退出/回收时刷 Unclosed 告警。

契约：
  1. endpoint 取用走短生命周期客户端，**成功与异常都必须 close**；
  2. 返回值与 `(url, error_code)` 语义不变。
"""
import asyncio

import pytest

from src.domains.runtime.im.adapters.feishu import channel as feishu_channel
from src.domains.runtime.im import feishu_async


class FakeApi:
    instances = []
    raise_on_fetch = False

    def __init__(self, *a, **kw):
        self.closed = False
        FakeApi.instances.append(self)

    async def get_ws_endpoint(self, ctx):
        if FakeApi.raise_on_fetch:
            raise RuntimeError("endpoint boom")
        return "wss://example.invalid/ws", 0

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_api(monkeypatch):
    FakeApi.instances = []
    FakeApi.raise_on_fetch = False
    monkeypatch.setattr(feishu_async, "FeishuAsync", FakeApi)
    yield


def test_endpoint_fetcher_closes_session_on_success():
    url, code = asyncio.run(feishu_channel._default_endpoint_fetcher("cli_x", "secret"))
    assert (url, code) == ("wss://example.invalid/ws", 0)
    assert FakeApi.instances and FakeApi.instances[0].closed


def test_endpoint_fetcher_closes_session_on_error():
    FakeApi.raise_on_fetch = True
    with pytest.raises(RuntimeError):
        asyncio.run(feishu_channel._default_endpoint_fetcher("cli_x", "secret"))
    assert FakeApi.instances and FakeApi.instances[0].closed
