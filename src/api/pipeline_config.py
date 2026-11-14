"""Compatibility alias (P1 模块归位): canonical module src.domains.mgmt.pipeline_config.

流水线配置（devops_pipeline_scene / scene_market 等）是管理域数据，实现迁入 mgmt 域。
旧路径以 sys.modules 别名指回 canonical 模块（import / monkeypatch / 属性赋值作用在同一模块对象上）。
"""
import sys as _sys

import src.domains.mgmt.pipeline_config as _impl

_sys.modules[__name__] = _impl
