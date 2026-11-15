"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.scene_precheck.

场景预检读运行域数据（agent_runtimes / runtime_credentials / runtime_health_scores），
实现迁入 runtime 域；旧路径以 sys.modules 别名指回 canonical 模块。
"""
import sys as _sys

import src.domains.runtime.scene_precheck as _impl

_sys.modules[__name__] = _impl
