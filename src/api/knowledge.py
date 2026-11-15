"""Compatibility alias (P1 模块归位): canonical module src.domains.mgmt.knowledge.

知识索引/正文读写属管理域数据（agent_shared_knowledge / scene_knowledge_ref /
knowledge_write_queue / managed_repos 均 mgmt 归属），实现整体迁入 mgmt 域；
旧路径 `src.api.knowledge` 以 sys.modules 别名指回 canonical 模块，
保证 import / monkeypatch / 属性赋值作用在同一个模块对象上。
"""
import sys as _sys

import src.domains.mgmt.knowledge as _impl

_sys.modules[__name__] = _impl
