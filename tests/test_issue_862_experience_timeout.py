"""Issue #862 回归测试：体验模板创建超时闸 30→45（防回退）。

背景：#357 P1-C 引入 30s 超时闸；domain-swarm 全流程实测 ≈30s 压线偶发 504。
P0 止血：timeout 提升至 45 并常量化（EXPERIENCE_TEMPLATE_CREATE_TIMEOUT）。
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api.routes.scenes import EXPERIENCE_TEMPLATE_CREATE_TIMEOUT  # noqa: E402
from src.api.routes import scenes as scenes_routes  # noqa: E402


class TestExperienceTimeoutGate:
    def test_timeout_constant_value(self):
        """闸门值必须为 45：覆盖 ≈30s 实测全流程 + 抖动余量（#862 M1）。"""
        assert EXPERIENCE_TEMPLATE_CREATE_TIMEOUT == 45

    def test_timeout_constant_not_regressed_to_30(self):
        """显式防回退到 #357 误伤值 30。"""
        assert EXPERIENCE_TEMPLATE_CREATE_TIMEOUT != 30

    def test_route_uses_named_constant(self):
        """experience_market_scene 必须引用命名常量而非内联魔数。"""
        src = inspect.getsource(scenes_routes.experience_market_scene)
        assert "EXPERIENCE_TEMPLATE_CREATE_TIMEOUT" in src
        assert "timeout=30" not in src.replace("timeout=EXPERIENCE_TEMPLATE_CREATE_TIMEOUT", "")

    def test_timeout_below_outer_limits(self):
        """45s 必须低于外层 nginx 300s / 前端 300s 覆盖（契约核对）。"""
        assert EXPERIENCE_TEMPLATE_CREATE_TIMEOUT < 300
