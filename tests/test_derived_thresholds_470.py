"""Issue #470: 服务端派生阈值 getter 化失败测试（红）。

stuck 预警（22min）/ 孤儿回收（6h）原先独立写死于：
- src/observability/__init__.py:462 `_STUCK_MINUTES = 22`
- src/api/routes/dashboard.py:134 `timedelta(minutes=22)`
- src/runtime/cleanup.py:23 `_LONG_RUNNING_MINUTES = 360`（env 兜底保留）

改造后均从 src/config.get_stuck_threshold_minutes() / get_orphan_threshold_minutes()
派生（随统一超时配置缩放）。本文件断言各模块暴露的取值入口跟随配置变化。
"""
import os
import sys

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.config import _write_runtime_config_for_test, RUNTIME_CONFIG_PATH  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    monkeypatch.delenv("SWEEPER_LONG_RUNNING_MINUTES", raising=False)
    from src.user.db import set_meta_value
    set_meta_value("runtime_task_timeout", "")
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    yield
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    from src.config import invalidate_runtime_config_cache
    invalidate_runtime_config_cache()


class TestObservabilityStuck:
    """observability 采样器 stuck 阈值跟随统一配置。"""

    def test_default_22(self):
        import src.observability as obs
        assert obs._stuck_minutes() == 22

    def test_scales_with_timeout(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        import src.observability as obs
        assert obs._stuck_minutes() == 44


class TestDashboardStuck:
    """dashboard ops overview 的 stuck 口径跟随统一配置。"""

    def test_default_22(self):
        from src.api.routes.dashboard import _stuck_minutes
        assert _stuck_minutes() == 22

    def test_scales_with_timeout(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        from src.api.routes.dashboard import _stuck_minutes
        assert _stuck_minutes() == 44


class TestCleanupOrphan:
    """cleanup sweeper 孤儿阈值：统一配置派生 + SWEEPER env 兜底优先保留。"""

    def test_default_360(self):
        from src.runtime.cleanup import _long_running_minutes
        assert _long_running_minutes() == 360

    def test_scales_with_timeout(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        from src.runtime.cleanup import _long_running_minutes
        assert _long_running_minutes() == 720

    def test_sweeper_env_still_overrides(self, monkeypatch):
        """既有 SWEEPER_LONG_RUNNING_MINUTES env 兜底语义保留（优先级高于派生值）。"""
        monkeypatch.setenv("SWEEPER_LONG_RUNNING_MINUTES", "120")
        from src.runtime.cleanup import _long_running_minutes
        assert _long_running_minutes() == 120
