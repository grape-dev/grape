"""Issue #470: worker 侧超时链路失败测试（红）。

覆盖 RUNTIME_TASK_TIMEOUT 从服务端配置到 worker 执行链的传递：
1. 服务端 enqueue_task 注入任务级 env（env_extra JSON 列）
2. hermes/hwcloud ACP `_global_timeout_for` 改为 env 优先（任务级 env >
   os.environ > 默认——与同文件 _silent_timeout_for(env) 模式一致，#378/#416 先例）
3. worker 看门狗（agent.py _wrap 内）任务级 env 优先
4. service.py 启动期耦合告警读统一 getter

全部针对纯函数/取值逻辑断言，不起 worker 进程。
"""
import os
import sys

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from src.runtime.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
import src.agent.hermes_acp as ha  # noqa: E402
import src.agent.hwcloud_acp as hca  # noqa: E402


class TestHermesGlobalTimeoutEnv:
    """hermes _global_timeout_for(max_exec, env)：任务级 env 优先。"""

    def test_env_dict_overrides_os_environ(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "7200")
        # 任务级 env（管理台/服务端注入）优先于 worker 本地 os.environ
        assert ha._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "1800"}) == 1800.0

    def test_env_dict_overrides_default(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert ha._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "5400"}) == 5400.0

    def test_no_env_falls_back_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "7200")
        assert ha._global_timeout_for(600) == 7200.0

    def test_floor_protection_with_env(self):
        # 非法/过小任务级 env：下限保护 max_exec_seconds
        assert ha._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "10"}) == 600.0

    def test_invalid_env_value_falls_back(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert ha._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "garbage"}) == 3600.0

    def test_none_env_keeps_legacy_behavior(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert ha._global_timeout_for(600, env=None) == 3600.0


class TestHwcloudGlobalTimeoutEnv:
    """hwcloud _global_timeout_for 同模式。"""

    def test_env_dict_overrides(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "7200")
        assert hca._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "1800"}) == 1800.0

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert hca._global_timeout_for(600, env={"RUNTIME_TASK_TIMEOUT": "x"}) == 3600.0


class TestWatchdogEnvPriority:
    """worker 看门狗（agent.py）：任务级 env 优先于 os.environ。

    seam：_watchdog_timeout_for(env) —— _wrap 内取 task_timeout 的取值函数。
    """

    def test_task_env_overrides_os_environ(self, monkeypatch):
        from src.runtime.agent import _watchdog_timeout_for
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "7200")
        assert _watchdog_timeout_for({"RUNTIME_TASK_TIMEOUT": "1800"}) == 1800.0

    def test_default_without_any_source(self, monkeypatch):
        from src.runtime.agent import _watchdog_timeout_for
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        assert _watchdog_timeout_for({}) == 3600.0

    def test_os_environ_when_task_env_missing(self, monkeypatch):
        from src.runtime.agent import _watchdog_timeout_for
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "900")
        assert _watchdog_timeout_for({}) == 900.0


class TestServerInjectsTimeoutIntoEnvExtra:
    """服务端 enqueue_task：把统一 getter 值 setdefault 进 env_extra。

    seam：_runtime_timeout_env_kv() —— 返回注入键值对的纯函数。
    worker 端 os.environ 已显式设置时不覆盖（setdefault 语义在 enqueue 落库前判断）。
    """

    def test_returns_timeout_kv(self, monkeypatch):
        from src.runtime.service import _runtime_timeout_env_kv
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "5400")
        assert _runtime_timeout_env_kv() == {"RUNTIME_TASK_TIMEOUT": "5400"}

    def test_uses_config_default_when_unset(self, monkeypatch):
        from src.runtime.service import _runtime_timeout_env_kv
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        # 无 meta/文件时 → 默认 3600（与现状一致）
        assert _runtime_timeout_env_kv() == {"RUNTIME_TASK_TIMEOUT": "3600"}
