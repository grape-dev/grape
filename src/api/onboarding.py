"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.onboarding.

Onboarding 任务操作的是运行域数据（skill / devops_agent_session / meta /
chat_conversations），实现整体迁入 runtime 域；旧路径以 sys.modules 别名指回
canonical 模块，保证 monkeypatch / 属性赋值作用在同一模块对象上。
"""
import sys as _sys

import src.domains.runtime.onboarding as _impl

_sys.modules[__name__] = _impl
