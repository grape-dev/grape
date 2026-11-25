"""P0-4（spec A4/A5）：executor.get_backend 显式失败语义。

根因：get_backend(name) 未注册时静默 fallback 到 opencode（executor.py 暗雷）——
descriptor 化迁移期撞此暗雷会「跑错引擎」而非「报错」，故障被吞成行为差异。
裁定（2026-11-13 spec §1.3）：显式失败语义在 get_backend_or_none 已局部存在，
P0a 把它扶正为主路径：get_backend 未注册必须抛错，错误消息带引擎名与已注册清单。

调用方对账（2026-11-13 核验）：
- executor.py:918 裸调（正是暗雷入口）→ 期望受益方
- cloud_worker.py:789 已 try/except 包裹 → 抛错后走 fail log，语义正好成立
- webhook.py:2445 走 vcs/adapter.py:49 同名 get_backend（不同函数，已显式 None 判定）→ 不受影响
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.domains.runtime.engine.registry  # noqa: E402,F401 先触发（绕开循环导入）
from src.domains.runtime.agents import executor


def test_get_backend_unknown_name_raises_with_registry_list():
    """A5：未注册引擎名 → 抛 KeyError，消息含该引擎名与已注册清单（不静默 fallback）。"""
    with pytest.raises(KeyError) as ei:
        executor.get_backend("no-such-engine")
    msg = str(ei.value)
    assert "no-such-engine" in msg
    # 消息里必须带已注册清单——排障时能直接看到合法值域
    for registered in ("opencode", "hermes", "hwcloud", "aishell-dsh"):
        assert registered in msg


def test_get_backend_registered_name_returns_backend():
    """已注册名正常返回（回归保护）。"""
    b = executor.get_backend("hermes")
    assert b is executor._BACKENDS["hermes"]


def test_get_backend_or_none_keeps_none_semantics():
    """get_backend_or_none 保留原语义：未注册/空名返回 None，不抛错。"""
    assert executor.get_backend_or_none("no-such-engine") is None
    assert executor.get_backend_or_none("") is None
    assert executor.get_backend_or_none(None) is None
    assert executor.get_backend_or_none("hermes") is not None


def test_no_silent_opencode_fallback():
    """暗雷回归守卫：拼写错的引擎名绝不能拿到 opencode 实例。"""
    with pytest.raises(KeyError):
        executor.get_backend("hermess")  # 多打一个 s，历史上会静默跑 opencode
