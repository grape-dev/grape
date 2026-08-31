"""web→飞书推送主事件循环投递测试（web-to-feishu-push-cross-loop-fix.md §7.1）"""
import asyncio
from unittest import mock

import pytest

from src.api.routes.chat import _dispatch_push_to_main_loop


class _BM:
    loop = None


def _fake_bm(loop):
    bm = _BM()
    bm.loop = loop
    return bm


@pytest.fixture
def fake_main_bm(monkeypatch):
    """mock src.api.main（chat.py helper 经 `from src import api` 取 api.main）"""
    m_main = mock.MagicMock()
    monkeypatch.setattr("src.api.main", m_main, raising=False)
    m_main.bot_manager = None
    return m_main


def _fake_running_loop():
    """返回一个有 is_running=True 的假 loop（避免真实 asyncio loop 生命周期问题）"""
    loop = mock.MagicMock()
    loop.is_running.return_value = True
    return loop


def test_dispatch_uses_run_coroutine_threadsafe_when_main_loop(monkeypatch, fake_main_bm):
    loop = _fake_running_loop()
    fake_main_bm.bot_manager = _fake_bm(loop)

    async def coro():
        pass

    with mock.patch("asyncio.run_coroutine_threadsafe") as m_rt:
        _dispatch_push_to_main_loop(coro())
        m_rt.assert_called_once()
        assert m_rt.call_args[0][1] is loop


def test_dispatch_fallback_asyncio_run_when_no_loop(monkeypatch, fake_main_bm):
    fake_main_bm.bot_manager = _fake_bm(None)  # loop = None

    async def coro():
        pass

    with mock.patch("asyncio.run") as m_run:
        _dispatch_push_to_main_loop(coro())
        m_run.assert_called_once()


def test_dispatch_no_bot_manager_falls_back(monkeypatch, fake_main_bm):
    async def coro():
        pass

    with mock.patch("asyncio.run") as m_run:
        _dispatch_push_to_main_loop(coro())
        m_run.assert_called_once()


def test_dispatch_handles_exception_in_coro(monkeypatch, fake_main_bm):
    loop = _fake_running_loop()
    fake_main_bm.bot_manager = _fake_bm(loop)

    async def boom():
        raise RuntimeError("x")

    with mock.patch("asyncio.run_coroutine_threadsafe") as m_rt:
        _dispatch_push_to_main_loop(boom())
        m_rt.assert_called_once()