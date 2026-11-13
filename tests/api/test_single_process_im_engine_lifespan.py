"""单进程入口（src/api/main.py）的生命周期必须负责 IM 引擎的启动与停机。

回归背景（PR #1377 原子回归报告 B3，high 风险面无实证）：
weekly-11 退役 legacy BotManager 后，server_tasks 在 engine_mode=on 时直接 skip，
而新引擎原先只在 grape_server.py（四进程 A 进程）里启动 → 单进程入口出现
「谁也不起」的空档：安装行 active、connected 恒 false、任何渠道都不拨号，
页面看板永远不亮。本 PR 在 main.py lifespan 里补了启动，此前没有任何断言看护。

本用例真跑一遍 lifespan（重活全部打桩），断言三件事：
  1. engine_mode=on → 引擎被启动，且同时挂到模块全局（routes/im.py:_engine 读它）
     与 app.state（关停钩子读它）；
  2. 退出 lifespan 时引擎被优雅停机（渠道长轮询协程要跟着收）；
  3. engine_mode=off → 不启动任何 IM 链路，也不往 app.state 挂引擎。
"""
import asyncio

import pytest
from fastapi import FastAPI


class _FakeEngine:
    instance_id = "test-instance"

    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _stub_heavy_startup(monkeypatch):
    """把 lifespan 里与 IM 无关的四组任务/后台循环全部打桩，只留待测分支。"""
    from src.api import server_tasks

    async def _start_stub(*_a, **_k):
        return {}

    async def _stop_stub(*_a, **_k):
        return None

    for name in ("start_mgmt_tasks", "start_event_tasks",
                 "start_worker_tasks", "start_insight_tasks"):
        monkeypatch.setattr(server_tasks, name, _start_stub, raising=False)
    for name in ("stop_mgmt_tasks", "stop_event_tasks",
                 "stop_worker_tasks", "stop_insight_tasks"):
        monkeypatch.setattr(server_tasks, name, _stop_stub, raising=False)

    async def _loop_stub(*_a, **_k):
        await asyncio.sleep(3600)

    # 各后台循环：import 在函数内，patch 模块属性即可生效
    import src.domains.runtime.engine.cleanup as _cleanup
    import src.api.patrol as _patrol
    import src.api.knowledge as _knowledge
    monkeypatch.setattr(_cleanup, "sweep_loop", _loop_stub, raising=False)
    monkeypatch.setattr(_patrol, "patrol_loop", _loop_stub, raising=False)
    monkeypatch.setattr(_knowledge, "knowledge_sync_loop", _loop_stub, raising=False)
    monkeypatch.setattr(_knowledge, "knowledge_write_worker", _loop_stub, raising=False)

    # outbox 消费者与各 scheduler：只关心「别起真东西」
    import src.domains.runtime.im.outbox as _outbox
    monkeypatch.setattr(_outbox, "attach_outbox_consumer", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(_outbox, "stop_outbox_consumer", lambda *_a, **_k: None,
                        raising=False)
    import src.domains.runtime.storage.skill_sync as _skill_sync
    monkeypatch.setattr(_skill_sync, "start_skill_sync_scheduler",
                        lambda *_a, **_k: None, raising=False)


def test_lifespan_starts_and_stops_im_engine(monkeypatch):
    from src.api import main as api_main
    from src.domains.runtime.im import engine_wiring

    _stub_heavy_startup(monkeypatch)

    engine = _FakeEngine()
    calls = {"start": 0}

    async def _fake_start(db_path=None):
        calls["start"] += 1
        return engine

    monkeypatch.setattr(engine_wiring, "engine_mode", lambda: "on", raising=False)
    monkeypatch.setattr(engine_wiring, "start_im_engine", _fake_start, raising=False)
    monkeypatch.setattr(api_main, "im_engine", None, raising=False)

    app = FastAPI()

    async def _run():
        async with api_main.lifespan(app):
            # 启动侧：模块全局与 app.state 双挂（两条读取路径）
            assert calls["start"] == 1, "engine_mode=on 时必须启动一次 IM 引擎"
            assert api_main.im_engine is engine, "模块全局未挂上引擎（routes/im.py 读它）"
            assert app.state.im_engine is engine, "app.state 未挂上引擎（关停钩子读它）"

    asyncio.run(_run())
    # 关停侧：lifespan 退出时必须优雅停引擎
    assert engine.stopped is True, "lifespan 退出时未停 IM 引擎（渠道协程会泄漏）"
    api_main.im_engine = None  # 复原模块全局，避免污染其它用例


def test_lifespan_skips_im_engine_when_mode_off(monkeypatch):
    from src.api import main as api_main
    from src.domains.runtime.im import engine_wiring

    _stub_heavy_startup(monkeypatch)

    calls = {"start": 0}

    async def _fake_start(db_path=None):
        calls["start"] += 1
        return _FakeEngine()

    monkeypatch.setattr(engine_wiring, "engine_mode", lambda: "off", raising=False)
    monkeypatch.setattr(engine_wiring, "start_im_engine", _fake_start, raising=False)
    monkeypatch.setattr(api_main, "im_engine", None, raising=False)

    app = FastAPI()

    async def _run():
        async with api_main.lifespan(app):
            assert calls["start"] == 0, "engine_mode=off 时不得启动 IM 引擎"
            assert getattr(app.state, "im_engine", None) is None

    asyncio.run(_run())
    api_main.im_engine = None