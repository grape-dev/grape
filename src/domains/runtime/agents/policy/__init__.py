"""策略门面——对外 API 导出 + 模块级 policy_manager。

与 sandbox/ 平行，互不 import。executor 是唯一拼接两者的层。

模块级默认实例用 Null（不检测），生产必须覆盖。
"""
from .manager import PolicyManager
from .types import Decision, InputDecision
from .errors import TaskBlocked, CommandBlocked

policy_manager = PolicyManager()

__all__ = [
    "policy_manager", "PolicyManager",
    "Decision", "InputDecision",
    "TaskBlocked", "CommandBlocked",
]
