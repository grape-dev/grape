"""Grape 源码包。

Issue #2010：``src/`` 顶层只保留本初始化文件与职责子包目录
（``api/``、``domains/``、``kernel/``、``repos/``、``authn/``、``help/``、``infra/``），
不再散置模块文件。

历史顶层模块路径（``src.config``、``src.lock`` 等，Issue #699 的薄兼容入口）
由本模块注册的懒加载别名解析到迁移后的子包位置，外部调用方无需改动：别名
``sys.modules`` 指向同一个模块对象，import / monkeypatch / 属性赋值语义不变。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

# 旧顶层模块路径 → Issue #2010 迁移后的子包路径（懒解析，不在 import src 时拉起依赖）。
LEGACY_MODULE_ALIASES = {
    "src.config": "src.infra.config",
    "src.featured_order": "src.infra.featured_order",
    "src.help_docs_sync": "src.help.help_docs_sync",
    "src.help_page_gen": "src.help.help_page_gen",
    "src.localfs": "src.infra.localfs",
    "src.lock": "src.infra.lock",
    "src.mysql_compat": "src.infra.mysql_compat",
    "src.outbound_ledger": "src.infra.outbound_ledger",
    "src.prompt_builder": "src.infra.prompt_builder",
    "src.repo": "src.repos.repo",
    "src.repo_cleanup": "src.repos.repo_cleanup",
    "src.repo_key": "src.repos.repo_key",
    "src.repo_migrate": "src.repos.repo_migrate",
    "src.scene_custom_env": "src.infra.scene_custom_env",
    "src.service_diagnostics": "src.infra.service_diagnostics",
    "src.session_manager": "src.authn.session_manager",
    "src.token_resolver": "src.authn.token_resolver",
    "src.token_service": "src.authn.token_service",
    "src.worker_package": "src.infra.worker_package",
}


class _LegacyAliasLoader(importlib.abc.Loader):
    """把旧路径加载为迁移后模块对象本身（非复制符号）。"""

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
