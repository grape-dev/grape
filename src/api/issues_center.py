"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.issues_center.

需求中心（issue 建档 / 状态流转 / 附件 / 任务对话 / 平台映射）读写运行域数据
（issue / events / agent_tasks），实现迁入 runtime 域。旧路径以 sys.modules 别名
指回 canonical 模块（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""
import sys as _sys

import src.domains.runtime.issues_center as _impl

_sys.modules[__name__] = _impl