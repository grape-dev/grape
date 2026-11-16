"""策略类型定义（零依赖）。"""
from __future__ import annotations
from enum import Enum


class Decision(Enum):
    """命令级决策——围绕「要不要改命令」展开。"""
    ALLOW = "allow"
    MODIFY = "modify"
    DENY = "deny"


class InputDecision(Enum):
    """输入级决策——围绕「要不要接受输入」展开。

    不用 Decision.MODIFY——「修改用户输入」语义不成立。
    """
    ALLOW = "allow"
    DENY = "deny"
