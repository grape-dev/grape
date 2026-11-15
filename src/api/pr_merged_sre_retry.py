"""Compatibility alias (P1 模块归位): canonical module src.domains.runtime.pr_merged_sre_retry.

PR 合并后的 SRE 重试链操作运行域数据（agent_tasks / events），实现迁入 runtime 域；
旧路径以 sys.modules 别名指回 canonical 模块（import / monkeypatch 同一对象）。
"""
import sys as _sys

import src.domains.runtime.pr_merged_sre_retry as _impl

_sys.modules[__name__] = _impl
