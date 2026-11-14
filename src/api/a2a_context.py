"""Compatibility alias (P1 模块归位): canonical module src.kernel.a2a_context.

A2A 事件上下文键（纯函数）。旧路径以 sys.modules 别名指回 canonical 模块
（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""
import sys as _sys

import src.kernel.a2a_context as _impl

_sys.modules[__name__] = _impl
