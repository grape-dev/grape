"""Compatibility alias (P1 模块归位): canonical module src.kernel.time_utils.

时间归一化工具（纯函数）。旧路径以 sys.modules 别名指回 canonical 模块
（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""
import sys as _sys

import src.kernel.time_utils as _impl

_sys.modules[__name__] = _impl
