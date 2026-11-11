"""Grape 源码包。

``src/`` 顶层只保留本初始化文件与三个职责包：``api/``（主控 HTTP 层）、
``domains/``（mgmt / runtime / insight 三个业务域 + contracts 共享契约层）、
``kernel/``（跨域共享的 canonical 基础设施实现）。Issue #2010 首轮的中间子包
（``repos/`` / ``authn/`` / ``help/`` / ``infra/``）已随收敛阶段移除：它们只承载
指向 ``src.kernel.*`` 的兼容 shim，多一层目录会把旧路径变成两跳。

历史顶层模块路径（``src.config``、``src.lock`` 等，Issue #699 的薄兼容入口）
由本模块注册的懒加载别名**直接**解析到 ``src/kernel/`` 的 canonical 实现，
外部调用方无需改动：别名 ``sys.modules`` 指向同一个模块对象，import /
monkeypatch / 属性赋值语义不变。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

# 旧顶层模块路径 → canonical 实现（一跳直达 src.kernel.*；懒解析，不在
# import src 时拉起依赖）。新增迁移模块只需在本表加一行。
LEGACY_MODULE_ALIASES = {
    "src.config": "src.kernel.config",
    "src.featured_order": "src.kernel.featured_order",
    "src.help_docs_sync": "src.kernel.help_docs_sync",
    "src.help_page_gen": "src.kernel.help_page_gen",
    "src.localfs": "src.kernel.localfs",
    "src.lock": "src.kernel.lock",
    "src.mysql_compat": "src.kernel.mysql_compat",
    "src.outbound_ledger": "src.kernel.outbound_ledger",
    "src.prompt_builder": "src.kernel.prompt_builder",
    "src.repo": "src.kernel.repo",
    "src.repo_cleanup": "src.kernel.repo_cleanup",
    "src.repo_key": "src.kernel.repo_key",
    "src.repo_migrate": "src.kernel.repo_migrate",
    "src.scene_custom_env": "src.kernel.scene_custom_env",
    "src.service_diagnostics": "src.kernel.service_diagnostics",
    "src.session_manager": "src.kernel.session_manager",
    "src.token_resolver": "src.kernel.token_resolver",
    "src.token_service": "src.kernel.token_service",
    "src.worker_package": "src.kernel.worker_package",
}


class _LegacyAliasLoader(importlib.abc.Loader):
    """把旧路径加载为 canonical 模块对象本身（非复制符号）。"""

    def __init__(self, target: str) -> None:
        self._target = target

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        target = importlib.import_module(self._target)
        sys.modules[module.__name__] = target


class _LegacyAliasFinder(importlib.abc.MetaPathFinder):
    """仅解析 ``LEGACY_MODULE_ALIASES`` 中的旧顶层路径；其余交给后续 finder。"""

    def find_spec(self, fullname, path=None, target=None):
        alias = LEGACY_MODULE_ALIASES.get(fullname)
        if alias is None:
            return None
        return importlib.util.spec_from_loader(fullname, _LegacyAliasLoader(alias))


def _install_legacy_aliases() -> None:
    # 追加到末尾：若将来原路径文件被重新加入，文件系统解析优先。
    if any(isinstance(finder, _LegacyAliasFinder) for finder in sys.meta_path):
        return
    sys.meta_path.append(_LegacyAliasFinder())


_install_legacy_aliases()
