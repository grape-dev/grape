"""安装行变更 → 立即唤醒扫描（凭证保存后不再等 30s 窗口）。

背景（2026-11-18 实障）：保存企微凭据后 WS 要等 supervisor 下一轮扫描（默认 30s）
才建连，期间第一条通知直接失败 "wecom long connection is not established"。
修法两条腿：①sync/revoke 成功后 kick 扫描；②transport 发送侧短暂等建连。
"""
import asyncio

from src.domains.runtime.im import engine_wiring
from src.domains.runtime.im.engine.supervisor import Supervisor
from src.domains.runtime.im.channel.registry import ChannelRegistry


class _FakeEngine:
    def __init__(self, result=True):
        self.calls = 0
        self._result = result

    def kick_scan(self) -> bool:
        self.calls += 1
        return self._result


def test_kick_engine_scan_routes_to_engine():
    engine = _FakeEngine()
    engine_wiring.set_current_engine(engine)
    try:
        assert engine_wiring.kick_engine_scan() is True
        assert engine.calls == 1
    finally:
        engine_wiring.set_current_engine(None)


def test_kick_engine_scan_without_engine_is_false_not_raise():
    engine_wiring.set_current_engine(None)
    assert engine_wiring.kick_engine_scan() is False


def test_kick_engine_scan_swallows_engine_errors():
    class _Boom:
        def kick_scan(self):
            raise RuntimeError("boom")

    engine_wiring.set_current_engine(_Boom())
    try:
        assert engine_wiring.kick_engine_scan() is False
    finally:
        engine_wiring.set_current_engine(None)


def test_im_engine_kick_scan_delegates_to_supervisor():
    kicked = []

    async def provider():
        return []

    async def scenario():
        sup = Supervisor(registry=ChannelRegistry(), instance_id="x",
                         installation_provider=provider, scan_interval=3600.0)
        await sup.start()
        await asyncio.sleep(0.02)
        eng = engine_wiring.ImEngine.__new__(engine_wiring.ImEngine)
        eng.supervisor = sup
        sup.kick = lambda: kicked.append(1) or True   # 只看委托，不重复验证 set
        assert eng.kick_scan() is True
        assert kicked == [1]
        await sup.stop()

    asyncio.run(scenario())
