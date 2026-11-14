"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.event_store.

事件存储操作运行域数据（events / agent_tasks / event_active_guard / event_skipped），实现迁入 runtime 域。旧路径以 sys.modules 别名指回 canonical 模块
（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""
import sys as _sys

import src.domains.runtime.event_store as _impl

_sys.modules[__name__] = _impl
