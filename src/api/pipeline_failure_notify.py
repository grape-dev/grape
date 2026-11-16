"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.pipeline_failure_notify.

失败节点通知读写运行域数据（events / issue / IM 绑定），实现迁入 runtime 域。
旧路径以 sys.modules 别名指回 canonical 模块。
"""
import sys as _sys

import src.domains.runtime.pipeline_failure_notify as _impl

_sys.modules[__name__] = _impl
